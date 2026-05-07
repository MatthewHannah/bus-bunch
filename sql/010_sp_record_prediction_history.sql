-- 010_sp_record_prediction_history.sql
-- ----------------------------------------------------------------------------
-- dbo.sp_RecordPredictionHistory @snapshot_ts
--
-- Captures every staging prediction for a tracked route into
-- dbo.prediction_history. Called by SnapshotWriter BEFORE
-- sp_ProcessPredictionPoll, inside the same transaction.
--
-- Intentionally has no awareness of:
--   * gap_too_large / gap_no_data / mass_dropout (we want MARTA's raw
--     behavior on weird polls — that IS the signal)
--   * the derive engine (our derivation logic is downstream interpretation;
--     this table is upstream raw data)
--   * resurrection / suspect_cancelled
--
-- Returns @@ROWCOUNT as `recorded_n` so the caller can log it.
-- ----------------------------------------------------------------------------

IF OBJECT_ID('dbo.sp_RecordPredictionHistory', 'P') IS NOT NULL
    DROP PROCEDURE dbo.sp_RecordPredictionHistory;
GO

CREATE PROCEDURE dbo.sp_RecordPredictionHistory
    @snapshot_ts datetime2(0)
AS
BEGIN
    SET NOCOUNT ON;

    -- Fast-path no-op when no routes are tracked. Avoids any read on
    -- staging_predictions (~17K rows) and any write transaction overhead.
    IF NOT EXISTS (SELECT 1 FROM dbo.tracked_route)
    BEGIN
        SELECT 0 AS recorded_n;
        RETURN;
    END

    INSERT INTO dbo.prediction_history
        (snapshot_ts, route_id, trip_id, trip_start_date, stop_sequence,
         stop_id, direction_id, vehicle_id,
         predicted_arrival_ts, schedule_relationship)
    SELECT
        @snapshot_ts, s.route_id, s.trip_id, s.trip_start_date, s.stop_sequence,
        s.stop_id, s.direction_id, s.vehicle_id,
        s.predicted_arrival_ts, s.schedule_relationship
    FROM dbo.staging_predictions s
    JOIN dbo.tracked_route t ON t.route_id = s.route_id;

    SELECT @@ROWCOUNT AS recorded_n;
END
GO
