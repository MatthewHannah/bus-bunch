-- 013_prediction_views_v2.sql
-- ----------------------------------------------------------------------------
-- vw_prediction_error v2: add scheduled_arrival_seconds.
--
-- Motivation: prediction_evolution.py and prediction_drift.py both re-issue
-- OR-of-ANDs queries against stop_arrival_event (already exposed by this
-- view) and scheduled_stop_time (not exposed). Adding scheduled arrival lets
-- both scripts collapse to a single query against this view and drop ~50
-- lines of SQL string-building each.
--
-- Convention: schedule times stay in their native form — seconds since the
-- service-day's local midnight, as stored in scheduled_stop_time.arrival_time.
-- May exceed 86400 for after-midnight trips. The viz layer combines this with
-- prediction_history.trip_start_date (and the agency's local timezone) to
-- produce a wall-clock arrival; this keeps the database UTC-only and avoids
-- baking the agency's tz into a view definition.
--
-- LEFT JOIN on scheduled_stop_time so ADDED / unscheduled trips still appear
-- (with NULL scheduled_arrival_seconds), matching the LEFT JOIN semantics
-- already used for stop_arrival_event.
-- ----------------------------------------------------------------------------

IF OBJECT_ID('dbo.vw_prediction_error', 'V') IS NOT NULL DROP VIEW dbo.vw_prediction_error;
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
    END AS arrival_resolution,
    -- Static-schedule arrival, in seconds since the service-day's local
    -- midnight (may exceed 86400). NULL for trips with no matching static
    -- row (ADDED / unscheduled). Combine with trip_start_date and the
    -- agency's tz on the consumer side to get a wall-clock arrival.
    sst.arrival_time AS scheduled_arrival_seconds
FROM dbo.prediction_history h
LEFT JOIN dbo.stop_arrival_event e
       ON e.trip_id         = h.trip_id
      AND e.trip_start_date = h.trip_start_date
      AND e.stop_sequence   = h.stop_sequence
LEFT JOIN dbo.scheduled_stop_time sst
       ON sst.trip_id       = h.trip_id
      AND sst.stop_sequence = h.stop_sequence
LEFT JOIN dbo.stop  s ON s.stop_id  = h.stop_id
LEFT JOIN dbo.route r ON r.route_id = h.route_id;
GO
