using System.Data;
using System.Globalization;
using System.IO.Compression;
using System.Net;
using CsvHelper;
using CsvHelper.Configuration;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Logging;

namespace BusBunch.Functions.Services;

/// <summary>
/// Downloads the agency's static GTFS bundle (a zip of CSV files) and
/// (re)loads the dim tables: stop, route, trip, scheduled_stop_time.
///
/// Strategy: full refresh in a single transaction.
///   1. download zip into memory (a few MiB)
///   2. open transaction
///   3. DELETE rows from sst, trip, route, stop (FK-respecting order)
///   4. SqlBulkCopy each table from its CSV
///   5. INSERT a row into gtfs_static_version
///   6. commit
///
/// MARTA's bundle is small (~5 MiB; ~250k stop_times rows). The full
/// refresh approach trades occasional brief table locks for vastly
/// simpler logic compared to delta-merging.
/// </summary>
public class StaticGtfsLoader
{
    private readonly IHttpClientFactory _httpFactory;
    private readonly GtfsOptions _options;
    private readonly SqlOptions _sql;
    private readonly ILogger<StaticGtfsLoader> _logger;

    public StaticGtfsLoader(
        IHttpClientFactory httpFactory,
        GtfsOptions options,
        SqlOptions sql,
        ILogger<StaticGtfsLoader> logger)
    {
        _httpFactory = httpFactory;
        _options = options;
        _sql = sql;
        _logger = logger;
    }

    public async Task<LoadResult> LoadAsync(CancellationToken ct)
    {
        var bytes = await DownloadZipWithRetryAsync(ct);
        var etag = _lastEtag;

        _logger.LogInformation("Downloaded {Bytes} bytes (etag={Etag})", bytes.Length, etag);

        using var zipStream = new MemoryStream(bytes);
        using var zip = new ZipArchive(zipStream, ZipArchiveMode.Read);

        var stops          = ReadStops(zip);
        var routes         = ReadRoutes(zip);
        var trips          = ReadTrips(zip);
        var stopTimes      = ReadStopTimes(zip);
        var (feedStart, feedEnd) = TryReadFeedDates(zip);

        await using var conn = new SqlConnection(_sql.ConnectionString);
        await conn.OpenAsync(ct);
        await using var tx = (SqlTransaction)await conn.BeginTransactionAsync(ct);

        try
        {
            // delete in FK-respecting order
            foreach (var t in new[] { "scheduled_stop_time", "trip", "route", "stop" })
            {
                await using var del = new SqlCommand($"DELETE FROM dbo.{t};", conn, tx)
                    { CommandTimeout = 120 };
                await del.ExecuteNonQueryAsync(ct);
            }

            await BulkCopyAsync(conn, tx, "dbo.stop",  stops, ct);
            await BulkCopyAsync(conn, tx, "dbo.route", routes, ct);
            await BulkCopyAsync(conn, tx, "dbo.trip",  trips, ct);
            await BulkCopyAsync(conn, tx, "dbo.scheduled_stop_time", stopTimes, ct);

            await using var ver = new SqlCommand(@"
                INSERT INTO dbo.gtfs_static_version
                    (loaded_at, feed_start_date, feed_end_date, source_etag)
                VALUES (SYSUTCDATETIME(), @s, @e, @t);", conn, tx);
            ver.Parameters.AddWithValue("@s", (object?)feedStart ?? DBNull.Value);
            ver.Parameters.AddWithValue("@e", (object?)feedEnd   ?? DBNull.Value);
            ver.Parameters.AddWithValue("@t", (object?)etag      ?? DBNull.Value);
            await ver.ExecuteNonQueryAsync(ct);

            await tx.CommitAsync(ct);
        }
        catch
        {
            await tx.RollbackAsync(ct);
            throw;
        }

        var result = new LoadResult(
            stops.Rows.Count, routes.Rows.Count, trips.Rows.Count, stopTimes.Rows.Count,
            feedStart, feedEnd, etag);
        _logger.LogInformation(
            "Loaded static GTFS: {Stops} stops, {Routes} routes, {Trips} trips, " +
            "{StopTimes} scheduled_stop_times (feed {Start}..{End})",
            result.Stops, result.Routes, result.Trips, result.StopTimes,
            result.FeedStart, result.FeedEnd);
        return result;
    }

    // -----------------------------------------------------------------------
    // Download with retry. MARTA's webserver returns intermittent 500s for
    // this public static feed, so we retry on any non-success plus transient
    // exceptions. Exponential backoff (2s, 4s, 8s, ...). Also accepts an
    // environment override GTFS_STATIC_LOCAL_ZIP for bootstrap/offline use.
    // -----------------------------------------------------------------------

    private string? _lastEtag;

    private async Task<byte[]> DownloadZipWithRetryAsync(CancellationToken ct)
    {
        var localPath = Environment.GetEnvironmentVariable("GTFS_STATIC_LOCAL_ZIP");
        if (!string.IsNullOrEmpty(localPath) && File.Exists(localPath))
        {
            _logger.LogInformation("Using local zip {Path} (override)", localPath);
            _lastEtag = null;
            return await File.ReadAllBytesAsync(localPath, ct);
        }

        var http = _httpFactory.CreateClient(GtfsRealtimeClient.HttpClientName);
        const int maxAttempts = 8;
        var delay = TimeSpan.FromSeconds(2);
        for (int attempt = 1; attempt <= maxAttempts; attempt++)
        {
            ct.ThrowIfCancellationRequested();
            try
            {
                _logger.LogInformation(
                    "Downloading static GTFS from {Url} (attempt {N}/{Max})",
                    _options.StaticUrl, attempt, maxAttempts);
                using var req = new HttpRequestMessage(HttpMethod.Get, _options.StaticUrl)
                {
                    // MARTA's origin returns 500 for HTTP/1.1 GETs but works
                    // over HTTP/2. Azure Blob (our fallback URL) only speaks
                    // HTTP/1.1. RequestVersionOrLower negotiates HTTP/2 via
                    // ALPN if the server supports it, otherwise falls back.
                    Version = HttpVersion.Version20,
                    VersionPolicy = HttpVersionPolicy.RequestVersionOrLower,
                };
                using var resp = await http.SendAsync(req, ct);
                if (resp.IsSuccessStatusCode)
                {
                    _lastEtag = resp.Headers.ETag?.Tag;
                    return await resp.Content.ReadAsByteArrayAsync(ct);
                }
                _logger.LogWarning(
                    "Static GTFS download returned {Status} on attempt {N}; retrying in {Sec}s",
                    (int)resp.StatusCode, attempt, delay.TotalSeconds);
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException && attempt < maxAttempts)
            {
                _logger.LogWarning(ex, "Transient error downloading static GTFS on attempt {N}", attempt);
            }
            if (attempt == maxAttempts) break;
            await Task.Delay(delay, ct);
            delay = TimeSpan.FromSeconds(Math.Min(delay.TotalSeconds * 2, 30));
        }
        throw new InvalidOperationException(
            $"Failed to download static GTFS from {_options.StaticUrl} after {maxAttempts} attempts");
    }


    private static DataTable ReadStops(ZipArchive zip)
    {
        var dt = NewTable(
            ("stop_id", typeof(string)), ("stop_code", typeof(string)),
            ("stop_name", typeof(string)), ("stop_lat", typeof(decimal)),
            ("stop_lon", typeof(decimal)), ("location_type", typeof(byte)),
            ("parent_station", typeof(string)));

        foreach (var row in ReadCsv(zip, "stops.txt"))
        {
            dt.Rows.Add(
                row["stop_id"],
                EmptyToNull(row.GetValueOrDefault("stop_code")),
                row["stop_name"],
                ParseDecimal(row.GetValueOrDefault("stop_lat")),
                ParseDecimal(row.GetValueOrDefault("stop_lon")),
                ParseByte(row.GetValueOrDefault("location_type")),
                EmptyToNull(row.GetValueOrDefault("parent_station")));
        }
        return dt;
    }

    private static DataTable ReadRoutes(ZipArchive zip)
    {
        var dt = NewTable(
            ("route_id", typeof(string)), ("route_short_name", typeof(string)),
            ("route_long_name", typeof(string)), ("route_type", typeof(byte)));

        foreach (var row in ReadCsv(zip, "routes.txt"))
        {
            dt.Rows.Add(
                row["route_id"],
                EmptyToNull(row.GetValueOrDefault("route_short_name")),
                EmptyToNull(row.GetValueOrDefault("route_long_name")),
                ParseByte(row.GetValueOrDefault("route_type")));
        }
        return dt;
    }

    private static DataTable ReadTrips(ZipArchive zip)
    {
        var dt = NewTable(
            ("trip_id", typeof(string)), ("route_id", typeof(string)),
            ("service_id", typeof(string)), ("direction_id", typeof(byte)),
            ("trip_headsign", typeof(string)), ("block_id", typeof(string)),
            ("shape_id", typeof(string)));

        foreach (var row in ReadCsv(zip, "trips.txt"))
        {
            dt.Rows.Add(
                row["trip_id"],
                row["route_id"],
                EmptyToNull(row.GetValueOrDefault("service_id")),
                ParseByte(row.GetValueOrDefault("direction_id")),
                EmptyToNull(row.GetValueOrDefault("trip_headsign")),
                EmptyToNull(row.GetValueOrDefault("block_id")),
                EmptyToNull(row.GetValueOrDefault("shape_id")));
        }
        return dt;
    }

    private static DataTable ReadStopTimes(ZipArchive zip)
    {
        var dt = NewTable(
            ("trip_id", typeof(string)), ("stop_sequence", typeof(short)),
            ("stop_id", typeof(string)), ("arrival_time", typeof(int)),
            ("departure_time", typeof(int)), ("pickup_type", typeof(byte)),
            ("drop_off_type", typeof(byte)));

        foreach (var row in ReadCsv(zip, "stop_times.txt"))
        {
            dt.Rows.Add(
                row["trip_id"],
                short.Parse(row["stop_sequence"], CultureInfo.InvariantCulture),
                row["stop_id"],
                ParseGtfsTimeToSeconds(row.GetValueOrDefault("arrival_time")),
                ParseGtfsTimeToSeconds(row.GetValueOrDefault("departure_time")),
                ParseByte(row.GetValueOrDefault("pickup_type")),
                ParseByte(row.GetValueOrDefault("drop_off_type")));
        }
        return dt;
    }

    private static (string? start, string? end) TryReadFeedDates(ZipArchive zip)
    {
        var entry = zip.GetEntry("feed_info.txt");
        if (entry is null) return (null, null);
        foreach (var row in ReadCsv(zip, "feed_info.txt"))
        {
            return (
                EmptyToNull(row.GetValueOrDefault("feed_start_date")) as string,
                EmptyToNull(row.GetValueOrDefault("feed_end_date"))   as string);
        }
        return (null, null);
    }

    // -----------------------------------------------------------------------
    // helpers
    // -----------------------------------------------------------------------

    private static IEnumerable<Dictionary<string, string>> ReadCsv(ZipArchive zip, string name)
    {
        var entry = zip.GetEntry(name)
            ?? throw new InvalidOperationException($"GTFS bundle missing {name}");
        using var stream = entry.Open();
        using var reader = new StreamReader(stream);
        using var csv = new CsvReader(reader, new CsvConfiguration(CultureInfo.InvariantCulture)
        {
            HasHeaderRecord = true,
            TrimOptions = TrimOptions.Trim,
            BadDataFound = null,
            MissingFieldFound = null,
        });
        csv.Read();
        csv.ReadHeader();
        var headers = csv.HeaderRecord ?? Array.Empty<string>();
        while (csv.Read())
        {
            var row = new Dictionary<string, string>(headers.Length, StringComparer.OrdinalIgnoreCase);
            foreach (var h in headers)
                row[h] = csv.GetField(h) ?? string.Empty;
            yield return row;
        }
    }

    private static DataTable NewTable(params (string Name, Type Type)[] cols)
    {
        var dt = new DataTable();
        foreach (var (n, t) in cols) dt.Columns.Add(n, t);
        return dt;
    }

    private static async Task BulkCopyAsync(
        SqlConnection conn, SqlTransaction tx, string table, DataTable dt, CancellationToken ct)
    {
        if (dt.Rows.Count == 0) return;
        using var bulk = new SqlBulkCopy(conn, SqlBulkCopyOptions.Default, tx)
        {
            DestinationTableName = table,
            BatchSize = 10000,
            BulkCopyTimeout = 300,
        };
        foreach (DataColumn c in dt.Columns)
            bulk.ColumnMappings.Add(c.ColumnName, c.ColumnName);
        await bulk.WriteToServerAsync(dt, ct);
    }

    private static object EmptyToNull(string? s) =>
        string.IsNullOrWhiteSpace(s) ? DBNull.Value : s;

    private static object ParseDecimal(string? s) =>
        decimal.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out var d)
            ? d : DBNull.Value;

    private static object ParseByte(string? s) =>
        byte.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out var b)
            ? b : DBNull.Value;

    /// <summary>
    /// GTFS times are HH:MM:SS where HH may exceed 23 (e.g. "25:30:00" for
    /// 1:30 AM the next service day). We store seconds-since-midnight.
    /// </summary>
    private static object ParseGtfsTimeToSeconds(string? s)
    {
        if (string.IsNullOrWhiteSpace(s)) return DBNull.Value;
        var parts = s.Split(':');
        if (parts.Length != 3) return DBNull.Value;
        if (!int.TryParse(parts[0], out var h)) return DBNull.Value;
        if (!int.TryParse(parts[1], out var m)) return DBNull.Value;
        if (!int.TryParse(parts[2], out var sec)) return DBNull.Value;
        return h * 3600 + m * 60 + sec;
    }
}

public sealed record LoadResult(
    int Stops,
    int Routes,
    int Trips,
    int StopTimes,
    string? FeedStart,
    string? FeedEnd,
    string? Etag);
