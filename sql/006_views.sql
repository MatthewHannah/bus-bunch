-- 006_views.sql
-- ----------------------------------------------------------------------------
-- Convenience views for analysis.
--
-- v3 changes vs the old views.sql:
--   * vw_live_eta no longer needs vw_latest_snapshot; trip_stop_prediction
--     IS the live state, so it just selects everything in there.
--   * vw_stop_headway_with_sched joins schedule via trip_id (matches across
--     static and realtime feeds), not route_id (which doesn't).
--   * vw_stop_headway exposes trip_id / prev_trip_id so downstream views
--     can look up the matching scheduled rows.
-- ----------------------------------------------------------------------------

IF OBJECT_ID('dbo.vw_latest_snapshot', 'V')         IS NOT NULL DROP VIEW dbo.vw_latest_snapshot;
IF OBJECT_ID('dbo.vw_live_eta', 'V')                IS NOT NULL DROP VIEW dbo.vw_live_eta;
IF OBJECT_ID('dbo.vw_stop_headway_with_sched', 'V') IS NOT NULL DROP VIEW dbo.vw_stop_headway_with_sched;
IF OBJECT_ID('dbo.vw_stop_trunk_headway', 'V')      IS NOT NULL DROP VIEW dbo.vw_stop_trunk_headway;
IF OBJECT_ID('dbo.vw_stop_headway', 'V')            IS NOT NULL DROP VIEW dbo.vw_stop_headway;
GO

-- ---------------------------------------------------------------------------
-- Most recent successful (non-skipped) poll.
-- ---------------------------------------------------------------------------
CREATE VIEW dbo.vw_latest_snapshot AS
SELECT TOP 1 *
FROM dbo.snapshot
WHERE skipped_reason IS NULL
ORDER BY snapshot_ts DESC;
GO

-- ---------------------------------------------------------------------------
-- vw_live_eta: the current prediction set, joined to stop names + route
-- short names. trip_stop_prediction IS live state, so no snapshot join.
-- ---------------------------------------------------------------------------
CREATE VIEW dbo.vw_live_eta AS
SELECT
    p.stop_id,
    s.stop_name,
    p.route_id,
    r.route_short_name,
    p.direction_id,
    p.vehicle_id,
    p.trip_id,
    p.predicted_arrival_ts,
    p.last_seen_ts,
    DATEDIFF(SECOND, SYSUTCDATETIME(), p.predicted_arrival_ts) AS eta_seconds
FROM dbo.trip_stop_prediction p
LEFT JOIN dbo.stop  s ON s.stop_id  = p.stop_id
LEFT JOIN dbo.route r ON r.route_id = p.route_id
WHERE p.predicted_arrival_ts > SYSUTCDATETIME();
GO

-- ---------------------------------------------------------------------------
-- vw_stop_headway: per-route headway between consecutive arrivals at a
-- stop. high/medium confidence only; suspect_cancelled excluded.
-- ---------------------------------------------------------------------------
CREATE VIEW dbo.vw_stop_headway AS
WITH a AS (
    SELECT
        e.stop_id,
        e.route_id,
        e.direction_id,
        e.vehicle_id,
        e.trip_id,
        e.observed_arrival_ts,
        LAG(e.observed_arrival_ts) OVER (
            PARTITION BY e.stop_id, e.route_id, e.direction_id
            ORDER BY e.observed_arrival_ts
        ) AS prev_arrival_ts,
        LAG(e.vehicle_id) OVER (
            PARTITION BY e.stop_id, e.route_id, e.direction_id
            ORDER BY e.observed_arrival_ts
        ) AS prev_vehicle_id,
        LAG(e.trip_id) OVER (
            PARTITION BY e.stop_id, e.route_id, e.direction_id
            ORDER BY e.observed_arrival_ts
        ) AS prev_trip_id
    FROM dbo.stop_arrival_event e
    WHERE e.confidence IN ('high','medium')
      AND e.derivation <> 'suspect_cancelled'
)
SELECT
    a.stop_id,
    a.route_id,
    a.direction_id,
    a.prev_vehicle_id,
    a.vehicle_id,
    a.prev_trip_id,
    a.trip_id,
    a.prev_arrival_ts,
    a.observed_arrival_ts,
    DATEDIFF(SECOND, a.prev_arrival_ts, a.observed_arrival_ts) AS headway_seconds
FROM a
WHERE a.prev_arrival_ts IS NOT NULL;
GO

-- ---------------------------------------------------------------------------
-- vw_stop_headway_with_sched: per-arrival-pair actual vs scheduled headway.
-- Joins schedule via trip_id (consistent across static and realtime feeds).
-- ---------------------------------------------------------------------------
CREATE VIEW dbo.vw_stop_headway_with_sched AS
SELECT
    h.stop_id,
    h.route_id,
    h.direction_id,
    h.prev_vehicle_id,
    h.vehicle_id,
    h.prev_trip_id,
    h.trip_id,
    h.prev_arrival_ts,
    h.observed_arrival_ts,
    h.headway_seconds,
    sst_curr.arrival_time - sst_prev.arrival_time AS scheduled_headway_seconds,
    CAST(h.headway_seconds AS float)
        / NULLIF(sst_curr.arrival_time - sst_prev.arrival_time, 0) AS bunching_ratio
FROM dbo.vw_stop_headway h
LEFT JOIN dbo.scheduled_stop_time sst_curr
       ON sst_curr.trip_id = h.trip_id
      AND sst_curr.stop_id = h.stop_id
LEFT JOIN dbo.scheduled_stop_time sst_prev
       ON sst_prev.trip_id = h.prev_trip_id
      AND sst_prev.stop_id = h.stop_id;
GO

-- ---------------------------------------------------------------------------
-- vw_stop_trunk_headway: route-agnostic headway at a stop. Useful on
-- trunk segments where rider experience is "time until ANY bus".
-- Does NOT filter on confidence — trunk analysis benefits from including
-- everything we can derive.
-- ---------------------------------------------------------------------------
CREATE VIEW dbo.vw_stop_trunk_headway AS
WITH a AS (
    SELECT
        e.stop_id,
        e.observed_arrival_ts,
        e.route_id,
        e.vehicle_id,
        LAG(e.observed_arrival_ts) OVER (PARTITION BY e.stop_id ORDER BY e.observed_arrival_ts) AS prev_arrival_ts,
        LAG(e.route_id)            OVER (PARTITION BY e.stop_id ORDER BY e.observed_arrival_ts) AS prev_route_id,
        LAG(e.vehicle_id)          OVER (PARTITION BY e.stop_id ORDER BY e.observed_arrival_ts) AS prev_vehicle_id
    FROM dbo.stop_arrival_event e
)
SELECT
    stop_id,
    prev_arrival_ts,
    observed_arrival_ts,
    prev_route_id,
    route_id,
    prev_vehicle_id,
    vehicle_id,
    DATEDIFF(SECOND, prev_arrival_ts, observed_arrival_ts) AS headway_seconds
FROM a
WHERE prev_arrival_ts IS NOT NULL;
GO
