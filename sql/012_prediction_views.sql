-- 012_prediction_views.sql
-- ----------------------------------------------------------------------------
-- Views over dbo.prediction_history for accuracy analysis.
--
-- vw_prediction_error: the main analytical surface. One row per recorded
-- prediction, with the eventual derived arrival LEFT JOINed on. Exposes
-- horizon (seconds before observed/predicted arrival) and signed error
-- (positive = predicted later than observed, i.e. MARTA was pessimistic).
--
-- arrival_resolution captures *why* a prediction may not have an
-- observation:
--   * 'matched'             — joined to a real arrival event
--   * 'suspect_cancelled'   — joined to a suspect-cancelled arrival
--                             (treated as low confidence; observed_arrival_ts
--                             is the snapshot ts, not a real arrival)
--   * 'unresolved'          — no arrival event found. Either the trip is
--                             still in progress, or the arrival window was
--                             skipped (gap_no_data / mass_dropout), or the
--                             trip was cancelled and never resurrected.
-- Filter on `arrival_resolution = 'matched'` for clean accuracy plots;
-- include 'unresolved' to inspect MARTA's prediction trajectories that
-- never resolved into an observation on our side.
--
-- vw_prediction_history_window: ops view — current size and date range,
-- so analysts know what data is queryable right now.
-- ----------------------------------------------------------------------------

IF OBJECT_ID('dbo.vw_prediction_error', 'V')           IS NOT NULL DROP VIEW dbo.vw_prediction_error;
IF OBJECT_ID('dbo.vw_prediction_history_window', 'V')  IS NOT NULL DROP VIEW dbo.vw_prediction_history_window;
GO

CREATE VIEW dbo.vw_prediction_error AS
SELECT
    h.snapshot_ts,
    h.route_id,
    r.route_short_name,
    h.trip_id,
    h.trip_start_date,
    h.stop_sequence,
    h.stop_id,
    s.stop_name,
    h.direction_id,
    h.vehicle_id,
    h.predicted_arrival_ts,
    h.schedule_relationship,
    e.observed_arrival_ts,
    e.derivation,
    e.confidence,
    -- horizon = how far ahead the prediction was looking. Use the observed
    -- arrival when we have one (true horizon); fall back to the predicted
    -- arrival for unresolved rows so the column isn't all NULL.
    DATEDIFF(SECOND, h.snapshot_ts,
             COALESCE(e.observed_arrival_ts, h.predicted_arrival_ts))
        AS horizon_seconds,
    -- signed error: positive = predicted later than observed (MARTA was
    -- pessimistic / bus came early); negative = bus came late.
    DATEDIFF(SECOND, e.observed_arrival_ts, h.predicted_arrival_ts)
        AS error_seconds,
    CASE
        WHEN e.event_id IS NULL                         THEN 'unresolved'
        WHEN e.derivation = 'suspect_cancelled'         THEN 'suspect_cancelled'
        ELSE                                                  'matched'
    END AS arrival_resolution
FROM dbo.prediction_history h
LEFT JOIN dbo.stop_arrival_event e
       ON e.trip_id         = h.trip_id
      AND e.trip_start_date = h.trip_start_date
      AND e.stop_sequence   = h.stop_sequence
LEFT JOIN dbo.stop  s ON s.stop_id  = h.stop_id
LEFT JOIN dbo.route r ON r.route_id = h.route_id;
GO

CREATE VIEW dbo.vw_prediction_history_window AS
SELECT
    (SELECT MIN(snapshot_ts) FROM dbo.prediction_history)   AS oldest_snapshot_ts,
    (SELECT MAX(snapshot_ts) FROM dbo.prediction_history)   AS newest_snapshot_ts,
    (SELECT COUNT_BIG(*)     FROM dbo.prediction_history)   AS row_count,
    (SELECT SUM(used_page_count) * 8 / 1024
       FROM sys.dm_db_partition_stats
       WHERE object_id = OBJECT_ID('dbo.prediction_history')) AS used_mb,
    (SELECT COUNT(*)         FROM dbo.tracked_route)        AS tracked_route_n;
GO
