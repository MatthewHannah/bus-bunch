using System.Data;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Logging;
using TransitRealtime;

namespace BusBunch.Functions.Services;

/// <summary>
/// Persists one MARTA poll into Azure SQL using the v3 upsert model:
///   1. SqlBulkCopy vehicle positions into dbo.vehicle_position_snapshot
///   2. SqlBulkCopy parsed trip predictions into dbo.staging_predictions
///   3. EXEC dbo.sp_ProcessPredictionPoll (MERGE → derive arrivals →
///      DELETE disappeared → INSERT snapshot heartbeat → TRUNCATE staging)
///
/// All three steps run inside a single transaction so a mid-poll failure
/// leaves no half-written state.
/// </summary>
public class SnapshotWriter
{
    private static readonly DateTime UnixEpoch =
        new(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);

    private readonly string _connectionString;
    private readonly ILogger<SnapshotWriter> _logger;

    public SnapshotWriter(SqlOptions options, ILogger<SnapshotWriter> logger)
    {
        _connectionString = options.ConnectionString;
        _logger = logger;
    }

    public async Task WriteAsync(
        DateTime snapshotTs,
        FeedMessage vehicleFeed,
        FeedMessage tripFeed,
        int pollDurationMs,
        CancellationToken ct)
    {
        // Truncate to whole seconds so the value matches our datetime2(0) columns.
        snapshotTs = new DateTime(
            snapshotTs.Year, snapshotTs.Month, snapshotTs.Day,
            snapshotTs.Hour, snapshotTs.Minute, snapshotTs.Second,
            DateTimeKind.Utc);

        var feedTs = EpochSecondsToUtc((long)tripFeed.Header.Timestamp);

        await using var conn = new SqlConnection(_connectionString);
        await conn.OpenAsync(ct);
        await using var tx = (SqlTransaction)await conn.BeginTransactionAsync(ct);

        try
        {
            await BulkCopyVehiclePositionsAsync(conn, tx, snapshotTs, vehicleFeed, ct);
            await BulkCopyStagingPredictionsAsync(conn, tx, tripFeed, ct);

            var result = await ExecProcessPollAsync(
                conn, tx, snapshotTs, feedTs, pollDurationMs,
                vehicleFeed.Entity.Count, tripFeed.Entity.Count, ct);

            await tx.CommitAsync(ct);

            if (result.SkippedReason is not null)
            {
                _logger.LogWarning(
                    "Snapshot {SnapTs:o} SKIPPED ({Reason}): gap={GapS}s, prev={PrevTs:o}",
                    snapshotTs, result.SkippedReason, result.GapS, result.PrevSnapshotTs);
            }
            else
            {
                _logger.LogInformation(
                    "Snapshot {SnapTs:o}: {Up} upsert, {Dis} disappeared, {Der} derived, {Veh} vehicles, gap={GapS}s",
                    snapshotTs, result.UpsertN, result.DisappearedN, result.DerivedN,
                    vehicleFeed.Entity.Count, result.GapS);
            }
        }
        catch
        {
            await tx.RollbackAsync(ct);
            throw;
        }
    }

    // -----------------------------------------------------------------------
    // bulk copy: vehicle positions (direct insert into the live table)
    // -----------------------------------------------------------------------

    private static async Task BulkCopyVehiclePositionsAsync(
        SqlConnection conn, SqlTransaction tx,
        DateTime snapshotTs, FeedMessage vehicleFeed,
        CancellationToken ct)
    {
        var dt = new DataTable();
        dt.Columns.Add("snapshot_ts",  typeof(DateTime));
        dt.Columns.Add("vehicle_id",   typeof(string));
        dt.Columns.Add("route_id",     typeof(string));
        dt.Columns.Add("direction_id", typeof(byte));
        dt.Columns.Add("trip_id",      typeof(string));
        dt.Columns.Add("latitude",     typeof(decimal));
        dt.Columns.Add("longitude",    typeof(decimal));
        dt.Columns.Add("bearing",      typeof(short));
        dt.Columns.Add("speed_mps",    typeof(decimal));
        dt.Columns.Add("veh_ts",       typeof(DateTime));

        var seen = new HashSet<string>(StringComparer.Ordinal);

        foreach (var ent in vehicleFeed.Entity)
        {
            if (ent.Vehicle is null) continue;
            var v = ent.Vehicle;
            var vid = v.Vehicle?.Id;
            if (string.IsNullOrEmpty(vid)) continue;
            // Skip scheduled-but-not-yet-running ghost vehicles (see NullableVehicleId).
            if (vid.StartsWith("block_", StringComparison.Ordinal)) continue;
            if (vid.Contains("schedBasedVehicle", StringComparison.Ordinal)) continue;
            if (!seen.Add(vid)) continue;

            dt.Rows.Add(
                snapshotTs,
                vid,
                Nullable(v.Trip?.RouteId),
                v.Trip is { HasDirectionId: true } ? (object)(byte)v.Trip.DirectionId : DBNull.Value,
                Nullable(v.Trip?.TripId),
                v.Position is { HasLatitude: true }  ? (object)(decimal)v.Position.Latitude  : DBNull.Value,
                v.Position is { HasLongitude: true } ? (object)(decimal)v.Position.Longitude : DBNull.Value,
                v.Position is { HasBearing: true }   ? (object)(short)v.Position.Bearing     : DBNull.Value,
                v.Position is { HasSpeed: true }     ? (object)(decimal)v.Position.Speed     : DBNull.Value,
                v.HasTimestamp ? (object)EpochSecondsToUtc((long)v.Timestamp) : DBNull.Value
            );
        }

        await BulkCopyAsync(conn, tx, "dbo.vehicle_position_snapshot", dt, ct);
    }

    // -----------------------------------------------------------------------
    // bulk copy: trip predictions into the staging table
    // -----------------------------------------------------------------------

    private static async Task BulkCopyStagingPredictionsAsync(
        SqlConnection conn, SqlTransaction tx,
        FeedMessage tripFeed,
        CancellationToken ct)
    {
        var dt = new DataTable();
        dt.Columns.Add("trip_id",                typeof(string));
        dt.Columns.Add("stop_sequence",          typeof(short));
        dt.Columns.Add("stop_id",                typeof(string));
        dt.Columns.Add("route_id",               typeof(string));
        dt.Columns.Add("direction_id",           typeof(byte));
        dt.Columns.Add("vehicle_id",             typeof(string));
        dt.Columns.Add("trip_start_date",        typeof(string));
        dt.Columns.Add("predicted_arrival_ts",   typeof(DateTime));
        dt.Columns.Add("predicted_departure_ts", typeof(DateTime));
        dt.Columns.Add("schedule_relationship",  typeof(byte));

        // staging_predictions is keyed on (trip_id, stop_sequence); dedup
        // here so SqlBulkCopy doesn't blow up on PK violations.
        var seen = new HashSet<(string, int)>();

        foreach (var ent in tripFeed.Entity)
        {
            if (ent.TripUpdate is null) continue;
            var tu = ent.TripUpdate;
            var tripId = tu.Trip?.TripId;
            if (string.IsNullOrEmpty(tripId)) continue;

            var routeId   = tu.Trip?.RouteId ?? "";
            var dirId     = tu.Trip is { HasDirectionId: true } ? (object)(byte)tu.Trip.DirectionId : DBNull.Value;
            var startDate = tu.Trip?.StartDate ?? "";   // NOT NULL in schema; '' sentinel
            var vehicleId = NullableVehicleId(tu.Vehicle?.Id);

            foreach (var stu in tu.StopTimeUpdate)
            {
                if (!stu.HasStopSequence) continue;
                var key = (tripId, (int)stu.StopSequence);
                if (!seen.Add(key)) continue;

                DateTime? arrTs = stu.Arrival is { HasTime: true }
                    ? EpochSecondsToUtc(stu.Arrival.Time) : null;
                DateTime? depTs = stu.Departure is { HasTime: true }
                    ? EpochSecondsToUtc(stu.Departure.Time) : null;

                dt.Rows.Add(
                    tripId,
                    (short)stu.StopSequence,
                    stu.StopId ?? "",
                    routeId,
                    dirId,
                    vehicleId,
                    startDate,
                    (object?)arrTs ?? DBNull.Value,
                    (object?)depTs ?? DBNull.Value,
                    stu.HasScheduleRelationship ? (object)(byte)stu.ScheduleRelationship : DBNull.Value
                );
            }
        }

        await BulkCopyAsync(conn, tx, "dbo.staging_predictions", dt, ct);
    }

    private static async Task BulkCopyAsync(
        SqlConnection conn, SqlTransaction tx, string table, DataTable dt,
        CancellationToken ct)
    {
        if (dt.Rows.Count == 0) return;
        using var bulk = new SqlBulkCopy(conn, SqlBulkCopyOptions.Default, tx)
        {
            DestinationTableName = table,
            BatchSize = 5000,
            BulkCopyTimeout = 60
        };
        foreach (DataColumn c in dt.Columns)
            bulk.ColumnMappings.Add(c.ColumnName, c.ColumnName);
        await bulk.WriteToServerAsync(dt, ct);
    }

    // -----------------------------------------------------------------------
    // proc invocation
    // -----------------------------------------------------------------------

    private static async Task<PollResult> ExecProcessPollAsync(
        SqlConnection conn, SqlTransaction tx,
        DateTime snapshotTs, DateTime feedTs, int pollDurationMs,
        int vehicleEntityN, int tripEntityN,
        CancellationToken ct)
    {
        await using var cmd = new SqlCommand("dbo.sp_ProcessPredictionPoll", conn, tx)
        {
            CommandType = CommandType.StoredProcedure,
            CommandTimeout = 60,
        };
        cmd.Parameters.Add("@snapshot_ts",      SqlDbType.DateTime2, 0).Value = snapshotTs;
        cmd.Parameters.Add("@feed_ts",          SqlDbType.DateTime2, 0).Value = feedTs;
        cmd.Parameters.Add("@poll_duration_ms", SqlDbType.Int).Value          = pollDurationMs;
        cmd.Parameters.Add("@vehicle_entity_n", SqlDbType.Int).Value          = vehicleEntityN;
        cmd.Parameters.Add("@trip_entity_n",    SqlDbType.Int).Value          = tripEntityN;

        await using var reader = await cmd.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct))
            throw new InvalidOperationException("sp_ProcessPredictionPoll returned no rows");

        return new PollResult(
            SnapshotTs:     reader.GetDateTime(0),
            PrevSnapshotTs: reader.IsDBNull(1) ? null : reader.GetDateTime(1),
            GapS:           reader.IsDBNull(2) ? null : reader.GetInt32(2),
            UpsertN:        reader.GetInt32(3),
            DisappearedN:   reader.GetInt32(4),
            DerivedN:       reader.GetInt32(5),
            SkippedReason:  reader.IsDBNull(6) ? null : reader.GetString(6));
    }

    // -----------------------------------------------------------------------

    private static DateTime EpochSecondsToUtc(long sec) => UnixEpoch.AddSeconds(sec);
    private static object Nullable(string? s) => string.IsNullOrEmpty(s) ? DBNull.Value : s;

    // Some MARTA trip_updates carry a synthetic vehicle.id like
    // "block_1223375_schedBasedVehicle" for scheduled-but-not-yet-running trips.
    // These are not real buses, and they also blow past our 16-char column cap,
    // so we treat them as "no assigned vehicle".
    private static object NullableVehicleId(string? s)
    {
        if (string.IsNullOrEmpty(s)) return DBNull.Value;
        if (s.StartsWith("block_", StringComparison.Ordinal)) return DBNull.Value;
        if (s.Contains("schedBasedVehicle", StringComparison.Ordinal)) return DBNull.Value;
        return s;
    }
}

public sealed class SqlOptions
{
    public string ConnectionString { get; init; } = default!;
}

public sealed record PollResult(
    DateTime  SnapshotTs,
    DateTime? PrevSnapshotTs,
    int?      GapS,
    int       UpsertN,
    int       DisappearedN,
    int       DerivedN,
    string?   SkippedReason);
