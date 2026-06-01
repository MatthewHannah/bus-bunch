using BusBunch.Functions.Services;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Extensions.Caching.Memory;
using Microsoft.Extensions.Logging;

namespace BusBunch.Functions.Functions.Api;

/// <summary>
/// Read-only HTTP endpoints that power the static visualization site.
///
/// These endpoints are public (<see cref="AuthorizationLevel.Anonymous"/>)
/// because the site is open. They read only from the views and dimension
/// tables defined under <c>sql/</c>. Every query is parameterized.
///
/// All endpoints are namespaced under <c>/api/</c> (the default Functions
/// route prefix). The companion frontend lives in <c>web/</c>.
/// </summary>
public class VisualizationApi
{
    private readonly SqlReader _sql;
    private readonly IMemoryCache _cache;
    private readonly ILogger<VisualizationApi> _logger;

    // Short TTL so freshly-active routes show up in the picker within a few
    // minutes, but the cold-path scans only run once per window per host.
    private static readonly TimeSpan PickerCacheTtl = TimeSpan.FromMinutes(5);

    public VisualizationApi(SqlReader sql, IMemoryCache cache, ILogger<VisualizationApi> logger)
    {
        _sql = sql;
        _cache = cache;
        _logger = logger;
    }

    // ----------------------------------------------------------------------
    // Pickers / metadata
    // ----------------------------------------------------------------------

    /// <summary>
    /// Routes available for the picker. We return the full <c>dbo.route</c>
    /// dim (60-ish rows) rather than filtering to "recently seen" routes —
    /// neither index on <c>stop_arrival_event</c> can satisfy a time-only
    /// recency probe as a seek, so any variant of that filter was costing
    /// 17–60s. The dim is tiny and a few stale rows in the dropdown is a
    /// much smaller UX hit than the spinner was. Cached briefly so even the
    /// dim hit only fires once per <see cref="PickerCacheTtl"/> per host.
    /// </summary>
    [Function("GetRoutes")]
    public async Task<IActionResult> GetRoutes(
        [HttpTrigger(AuthorizationLevel.Anonymous, "get", Route = "routes")] HttpRequest req,
        CancellationToken ct)
    {
        var rows = await GetOrCreatePickerAsync("viz:routes", async () =>
        {
            const string sql = @"
                SELECT
                    r.route_id,
                    r.route_short_name,
                    r.route_long_name
                FROM dbo.route r
                WHERE r.route_short_name IS NOT NULL
                ORDER BY
                    TRY_CONVERT(int, r.route_short_name),
                    r.route_short_name;";

            return await _sql.QueryAsync(sql, null, ct);
        });
        return ApiHelpers.JsonOk(rows);
    }

    /// <summary>
    /// Routes currently in <c>dbo.tracked_route</c>. The prediction error
    /// and evolution pages gate the route filter on this since only tracked
    /// routes have prediction history captured.
    /// </summary>
    [Function("GetTrackedRoutes")]
    public async Task<IActionResult> GetTrackedRoutes(
        [HttpTrigger(AuthorizationLevel.Anonymous, "get", Route = "tracked-routes")] HttpRequest req,
        CancellationToken ct)
    {
        var rows = await GetOrCreatePickerAsync("viz:tracked-routes", async () =>
        {
            const string sql = @"
                SELECT tr.route_id, r.route_short_name, r.route_long_name, tr.added_at
                FROM dbo.tracked_route tr
                LEFT JOIN dbo.route r ON r.route_id = tr.route_id
                ORDER BY
                    TRY_CONVERT(int, r.route_short_name),
                    r.route_short_name;";

            return await _sql.QueryAsync(sql, null, ct);
        });
        return ApiHelpers.JsonOk(rows);
    }

    // IMemoryCache.GetOrCreateAsync caches the returned Task — so if the
    // factory throws, the faulted Task is cached and every subsequent caller
    // re-throws the original exception until the TTL expires. We evict on
    // failure so retries actually re-run the query.
    private async Task<List<Dictionary<string, object?>>> GetOrCreatePickerAsync(
        string key,
        Func<Task<List<Dictionary<string, object?>>>> factory)
    {
        if (_cache.TryGetValue(key, out List<Dictionary<string, object?>>? cached) && cached is not null)
        {
            return cached;
        }

        try
        {
            var rows = await factory();
            _cache.Set(key, rows, PickerCacheTtl);
            return rows;
        }
        catch
        {
            _cache.Remove(key);
            throw;
        }
    }

    /// <summary>
    /// Stops on the canonical "spine" trip for a (route_short_name, direction)
    /// — mirrors the spine picker in <c>scripts/viz/marey.py</c>. Useful for
    /// the prediction error/evolution pages so users can pick a stop_id from
    /// a list rather than typing one in.
    /// </summary>
    [Function("GetStops")]
    public async Task<IActionResult> GetStops(
        [HttpTrigger(AuthorizationLevel.Anonymous, "get", Route = "stops")] HttpRequest req,
        CancellationToken ct)
    {
        var routeShortName = ApiHelpers.GetQuery(req, "route_short_name");
        var directionId    = ApiHelpers.GetQueryNullableInt(req, "direction_id");
        if (routeShortName is null)
        {
            return ApiHelpers.BadRequest("route_short_name is required");
        }

        const string sql = @"
            WITH candidate_trip AS (
                SELECT TOP 1 t.trip_id
                FROM dbo.trip t
                JOIN dbo.route r ON r.route_id = t.route_id
                JOIN dbo.scheduled_stop_time sst ON sst.trip_id = t.trip_id
                WHERE r.route_short_name = @route_short_name
                  AND (@direction_id IS NULL OR t.direction_id = @direction_id)
                GROUP BY t.trip_id
                ORDER BY COUNT(*) DESC
            )
            SELECT sst.stop_sequence, sst.stop_id, s.stop_name
            FROM candidate_trip ct
            JOIN dbo.scheduled_stop_time sst ON sst.trip_id = ct.trip_id
            JOIN dbo.stop s ON s.stop_id = sst.stop_id
            ORDER BY sst.stop_sequence;";

        var rows = await _sql.QueryAsync(sql, new (string, object?)[]
        {
            ("@route_short_name", routeShortName),
            ("@direction_id",     (object?)directionId),
        }, ct);
        return ApiHelpers.JsonOk(rows);
    }

    /// <summary>
    /// Busiest vehicles in the lookback window, descending by ping count.
    /// Mirrors the helper SELECT in <c>scripts/viz/vehicle_track.py</c>.
    ///
    /// Cached briefly because this is a multi-million-row aggregation over
    /// <c>vehicle_position_snapshot</c> (a 24h window can touch 5-9M rows)
    /// and the result barely shifts minute-to-minute. The cache key includes
    /// <paramref name="lookbackHours"/> so distinct windows don't collide.
    /// </summary>
    [Function("GetVehicles")]
    public async Task<IActionResult> GetVehicles(
        [HttpTrigger(AuthorizationLevel.Anonymous, "get", Route = "vehicles")] HttpRequest req,
        CancellationToken ct)
    {
        var lookbackHours = ApiHelpers.GetQueryInt(req, "lookback_hours", 24);
        if (lookbackHours is < 1 or > 168)
        {
            return ApiHelpers.BadRequest("lookback_hours must be between 1 and 168");
        }

        var rows = await GetOrCreatePickerAsync($"viz:vehicles:{lookbackHours}", async () =>
        {
            const string sql = @"
                SELECT TOP 200
                    v.vehicle_id,
                    COUNT(*)                AS ping_count,
                    MAX(v.snapshot_ts)      AS last_seen_ts,
                    MAX(v.route_id)         AS latest_route_id,
                    MAX(r.route_short_name) AS latest_route_short_name
                FROM dbo.vehicle_position_snapshot v
                LEFT JOIN dbo.route r ON r.route_id = v.route_id
                WHERE v.snapshot_ts > DATEADD(HOUR, -@lookback_hours, SYSUTCDATETIME())
                  AND v.latitude  IS NOT NULL
                  AND v.longitude IS NOT NULL
                GROUP BY v.vehicle_id
                ORDER BY ping_count DESC;";

            return await _sql.QueryAsync(sql,
                new (string, object?)[] { ("@lookback_hours", lookbackHours) }, ct);
        });
        return ApiHelpers.JsonOk(rows);
    }

    // ----------------------------------------------------------------------
    // Marey diagram
    // ----------------------------------------------------------------------

    /// <summary>
    /// Spine + arrivals for a marey plot. Single round-trip returns both the
    /// canonical stop sequence (Y axis) and every realtime arrival at any of
    /// those stops within the lookback window (the polylines).
    /// </summary>
    [Function("GetMarey")]
    public async Task<IActionResult> GetMarey(
        [HttpTrigger(AuthorizationLevel.Anonymous, "get", Route = "marey")] HttpRequest req,
        CancellationToken ct)
    {
        var routeShortName = ApiHelpers.GetQuery(req, "route_short_name");
        var directionId    = ApiHelpers.GetQueryNullableInt(req, "direction_id");
        var lookbackHours  = ApiHelpers.GetQueryInt(req, "lookback_hours", 24);

        if (routeShortName is null || directionId is null)
        {
            return ApiHelpers.BadRequest("route_short_name and direction_id are required");
        }
        if (lookbackHours is < 1 or > 168)
        {
            return ApiHelpers.BadRequest("lookback_hours must be between 1 and 168");
        }

        const string spineSql = @"
            WITH candidate_trip AS (
                SELECT TOP 1 t.trip_id
                FROM dbo.trip t
                JOIN dbo.route r ON r.route_id = t.route_id
                JOIN dbo.scheduled_stop_time sst ON sst.trip_id = t.trip_id
                WHERE r.route_short_name = @route_short_name
                  AND t.direction_id     = @direction_id
                GROUP BY t.trip_id
                ORDER BY COUNT(*) DESC
            )
            SELECT sst.stop_sequence, sst.stop_id, s.stop_name
            FROM candidate_trip ct
            JOIN dbo.scheduled_stop_time sst ON sst.trip_id = ct.trip_id
            JOIN dbo.stop s ON s.stop_id = sst.stop_id
            ORDER BY sst.stop_sequence;";

        var spine = await _sql.QueryAsync(spineSql, new (string, object?)[]
        {
            ("@route_short_name", routeShortName),
            ("@direction_id",     directionId),
        }, ct);

        if (spine.Count == 0)
        {
            return ApiHelpers.JsonOk(new { spine, arrivals = Array.Empty<object>() });
        }

        // Filter the arrivals query to just the spine's stops by re-running
        // the same candidate-trip CTE. Avoids dynamically expanding an
        // IN (...) list and keeps the whole query parameterized.
        const string arrivalsSql = @"
            WITH candidate_trip AS (
                SELECT TOP 1 t.trip_id
                FROM dbo.trip t
                JOIN dbo.route r ON r.route_id = t.route_id
                JOIN dbo.scheduled_stop_time sst ON sst.trip_id = t.trip_id
                WHERE r.route_short_name = @route_short_name
                  AND t.direction_id     = @direction_id
                GROUP BY t.trip_id
                ORDER BY COUNT(*) DESC
            ),
            spine_stops AS (
                SELECT DISTINCT sst.stop_id
                FROM candidate_trip ct
                JOIN dbo.scheduled_stop_time sst ON sst.trip_id = ct.trip_id
            )
            SELECT
                e.observed_arrival_ts,
                e.stop_id,
                e.route_id,
                e.direction_id,
                e.vehicle_id,
                e.trip_id
            FROM dbo.stop_arrival_event e
            JOIN dbo.route r ON r.route_id = e.route_id
            WHERE r.route_short_name = @route_short_name
              AND (e.direction_id = @direction_id OR e.direction_id IS NULL)
              AND e.stop_id IN (SELECT stop_id FROM spine_stops)
              AND e.observed_arrival_ts > DATEADD(HOUR, -@lookback_hours, SYSUTCDATETIME())
              AND e.confidence IN ('high','medium')
            ORDER BY e.vehicle_id, e.trip_id, e.observed_arrival_ts;";

        var arrivals = await _sql.QueryAsync(arrivalsSql, new (string, object?)[]
        {
            ("@route_short_name", routeShortName),
            ("@direction_id",     directionId),
            ("@lookback_hours",   lookbackHours),
        }, ct);

        return ApiHelpers.JsonOk(new { spine, arrivals });
    }

    // ----------------------------------------------------------------------
    // Vehicle track
    // ----------------------------------------------------------------------

    [Function("GetVehicleTrack")]
    public async Task<IActionResult> GetVehicleTrack(
        [HttpTrigger(AuthorizationLevel.Anonymous, "get", Route = "vehicle-track")] HttpRequest req,
        CancellationToken ct)
    {
        var vehicleId     = ApiHelpers.GetQuery(req, "vehicle_id");
        var lookbackHours = ApiHelpers.GetQueryInt(req, "lookback_hours", 24);

        if (vehicleId is null)
        {
            return ApiHelpers.BadRequest("vehicle_id is required");
        }
        if (lookbackHours is < 1 or > 168)
        {
            return ApiHelpers.BadRequest("lookback_hours must be between 1 and 168");
        }

        const string sql = @"
            SELECT
                v.snapshot_ts,
                v.vehicle_id,
                v.route_id,
                r.route_short_name,
                v.direction_id,
                v.trip_id,
                v.latitude,
                v.longitude,
                v.bearing,
                v.speed_mps
            FROM dbo.vehicle_position_snapshot v
            LEFT JOIN dbo.route r ON r.route_id = v.route_id
            WHERE v.vehicle_id = @vehicle_id
              AND v.snapshot_ts > DATEADD(HOUR, -@lookback_hours, SYSUTCDATETIME())
              AND v.latitude  IS NOT NULL
              AND v.longitude IS NOT NULL
            ORDER BY v.snapshot_ts;";

        var rows = await _sql.QueryAsync(sql, new (string, object?)[]
        {
            ("@vehicle_id",     vehicleId),
            ("@lookback_hours", lookbackHours),
        }, ct);
        return ApiHelpers.JsonOk(rows);
    }

    // ----------------------------------------------------------------------
    // Prediction error / evolution
    //
    // Both endpoints select essentially the same columns from
    // vw_prediction_error and let the frontend transform them — that matches
    // how the python scripts work and avoids server-side opinions about
    // (a) horizon thresholds and (b) ET-vs-UTC scheduled-arrival math.
    // ----------------------------------------------------------------------

    [Function("GetPredictionError")]
    public async Task<IActionResult> GetPredictionError(
        [HttpTrigger(AuthorizationLevel.Anonymous, "get", Route = "prediction-error")] HttpRequest req,
        CancellationToken ct)
    {
        return await QueryPredictionWindow(req, includeScheduled: false, ct);
    }

    [Function("GetPredictionEvolution")]
    public async Task<IActionResult> GetPredictionEvolution(
        [HttpTrigger(AuthorizationLevel.Anonymous, "get", Route = "prediction-evolution")] HttpRequest req,
        CancellationToken ct)
    {
        return await QueryPredictionWindow(req, includeScheduled: true, ct);
    }

    private async Task<IActionResult> QueryPredictionWindow(
        HttpRequest req, bool includeScheduled, CancellationToken ct)
    {
        var stopId         = ApiHelpers.GetQuery(req, "stop_id");
        var routeShortName = ApiHelpers.GetQuery(req, "route_short_name");
        var directionId    = ApiHelpers.GetQueryNullableInt(req, "direction_id");
        var startUtc       = ApiHelpers.GetQueryUtc(req, "start_utc");
        var endUtc         = ApiHelpers.GetQueryUtc(req, "end_utc");

        if (stopId is null || startUtc is null || endUtc is null)
        {
            return ApiHelpers.BadRequest(
                "stop_id, start_utc and end_utc are required (ISO 8601 UTC timestamps)");
        }
        if (endUtc <= startUtc)
        {
            return ApiHelpers.BadRequest("end_utc must be greater than start_utc");
        }
        // Cap window so a misclick doesn't try to materialize a week of
        // every-30-seconds prediction rows for every route.
        if ((endUtc.Value - startUtc.Value).TotalDays > 2.0)
        {
            return ApiHelpers.BadRequest("Window must be 48 hours or less");
        }

        var scheduledCol = includeScheduled ? ", v.scheduled_arrival_seconds" : "";
        var routeFilter = routeShortName is null
            ? ""
            : " AND v.route_short_name = @route_short_name" +
              " AND (v.direction_id = @direction_id OR v.direction_id IS NULL)";

        var sql = $@"
            SELECT
                v.snapshot_ts,
                v.route_id,
                v.route_short_name,
                v.direction_id,
                v.trip_id,
                v.trip_start_date,
                v.stop_sequence,
                v.stop_id,
                v.stop_name,
                v.vehicle_id,
                v.predicted_arrival_ts,
                v.observed_arrival_ts,
                v.derivation,
                v.confidence,
                v.horizon_seconds,
                v.error_seconds,
                v.arrival_resolution
                {scheduledCol}
            FROM dbo.vw_prediction_error v
            WHERE v.stop_id = @stop_id
              AND v.snapshot_ts >= @start_utc
              AND v.snapshot_ts <  @end_utc
              AND v.predicted_arrival_ts IS NOT NULL
              {routeFilter}
            ORDER BY v.route_id, v.trip_id, v.trip_start_date, v.snapshot_ts;";

        var parameters = new List<(string, object?)>
        {
            ("@stop_id",   stopId),
            ("@start_utc", startUtc),
            ("@end_utc",   endUtc),
        };
        if (routeShortName is not null)
        {
            parameters.Add(("@route_short_name", routeShortName));
            parameters.Add(("@direction_id",     (object?)directionId));
        }

        var rows = await _sql.QueryAsync(sql, parameters, ct);
        return ApiHelpers.JsonOk(rows);
    }
}
