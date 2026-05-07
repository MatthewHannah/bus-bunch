-- 007_resurrect_cancelled.sql
-- ----------------------------------------------------------------------------
-- Resurrect "suspect_cancelled" arrivals when MARTA un-cancels the trip
--
-- Background. When MARTA's tripupdates feed temporarily stops publishing
-- predictions for a trip mid-route (a feed glitch — verified happens in the
-- wild, e.g. trip 10784697 on 2026-04-23, where stops 10..73 vanished from
-- tripupdates in a single poll while vehiclepositions kept the bus driving),
-- our derive fires a 'suspect_cancelled' arrival event for every disappearing
-- stop. Those rows have:
--   - observed_arrival_ts pinned to the snapshot timestamp (NOT the real
--     arrival — the bus hasn't been there yet)
--   - confidence = 'low'
--
-- They're filtered out of the headway/marey views, so visually they're just
-- noise. The real problem is the UNIQUE dedup index
-- UX_arrival_dedupe (trip_id, trip_start_date, stop_sequence): when MARTA
-- later re-publishes the trip and the bus actually passes those stops, the
-- derive's NOT EXISTS guard sees the bogus 'suspect_cancelled' row and
-- silently skips the *real* 'dropped_from_upcoming' arrival. The trip is
-- permanently mute past the cancellation point.
--
-- This migration changes the proc so that on every poll, immediately after
-- the MERGE, we delete any 'suspect_cancelled' row whose (trip_id,
-- trip_start_date, stop_sequence) is back in staging_predictions. MARTA
-- changed its mind → undo our cancellation. The next time the bus really
-- passes the stop and the prediction disappears, the derive will INSERT a
-- proper arrival without the dedup blocking it.
--
-- Also adds a `resurrected_n` counter to dbo.snapshot for monitoring.
--
-- This is forward-only. Historical bogus rows stay; a separate cleanup pass
-- could remove them if desired (we can't reconstruct the real arrival times
-- since the prediction history isn't kept).
-- ----------------------------------------------------------------------------

-- 1. Add resurrected_n to the heartbeat. Nullable so old rows stay valid.
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.snapshot') AND name = 'resurrected_n'
)
BEGIN
    ALTER TABLE dbo.snapshot ADD resurrected_n int NULL;
END
GO

-- 2. Replace sp_ProcessPredictionPoll with a resurrection-aware version.
IF OBJECT_ID('dbo.sp_ProcessPredictionPoll', 'P') IS NOT NULL
    DROP PROCEDURE dbo.sp_ProcessPredictionPoll;
GO

CREATE PROCEDURE dbo.sp_ProcessPredictionPoll
    @snapshot_ts      datetime2(0),
    @feed_ts          datetime2(0),
    @poll_duration_ms int,
    @vehicle_entity_n int,
    @trip_entity_n    int
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @prev_snapshot_ts datetime2(0) = (
        SELECT MAX(snapshot_ts) FROM dbo.snapshot
        WHERE snapshot_ts < @snapshot_ts
          AND skipped_reason IS NULL
    );

    DECLARE @gap_s int =
        CASE WHEN @prev_snapshot_ts IS NULL THEN NULL
             ELSE DATEDIFF(SECOND, @prev_snapshot_ts, @snapshot_ts) END;

    DECLARE @staging_n int = (SELECT COUNT(*) FROM dbo.staging_predictions);
    DECLARE @live_n    int = (SELECT COUNT(*) FROM dbo.trip_stop_prediction);

    DECLARE @would_disappear int = (
        SELECT COUNT(*) FROM dbo.trip_stop_prediction p
        WHERE NOT EXISTS (
            SELECT 1 FROM dbo.staging_predictions s
            WHERE s.trip_id = p.trip_id AND s.stop_sequence = p.stop_sequence
        )
    );

    DECLARE @skipped_reason varchar(32) = NULL;
    IF @gap_s IS NOT NULL AND @gap_s > 300
        SET @skipped_reason = 'gap_too_large';
    ELSE IF @live_n > 0 AND @staging_n = 0
        SET @skipped_reason = 'mass_dropout';
    ELSE IF @live_n > 0 AND (1.0 * @would_disappear / @live_n) > 0.5
        SET @skipped_reason = 'mass_dropout';

    DECLARE @upsert_n      int = 0;
    DECLARE @disappeared_n int = 0;
    DECLARE @derived_n     int = 0;
    DECLARE @resurrected_n int = 0;

    IF @skipped_reason IS NULL
    BEGIN
        -- (a) MERGE staging into live
        MERGE dbo.trip_stop_prediction AS tgt
        USING dbo.staging_predictions  AS src
          ON  tgt.trip_id       = src.trip_id
          AND tgt.stop_sequence = src.stop_sequence
        WHEN MATCHED THEN UPDATE SET
            tgt.stop_id                = src.stop_id,
            tgt.route_id               = src.route_id,
            tgt.direction_id           = src.direction_id,
            tgt.vehicle_id             = src.vehicle_id,
            tgt.trip_start_date        = src.trip_start_date,
            tgt.predicted_arrival_ts   = src.predicted_arrival_ts,
            tgt.predicted_departure_ts = src.predicted_departure_ts,
            tgt.schedule_relationship  = src.schedule_relationship,
            tgt.last_seen_ts           = @snapshot_ts
        WHEN NOT MATCHED BY TARGET THEN INSERT
            (trip_id, stop_sequence, stop_id, route_id, direction_id, vehicle_id,
             trip_start_date, predicted_arrival_ts, predicted_departure_ts,
             schedule_relationship, first_seen_ts, last_seen_ts)
        VALUES
            (src.trip_id, src.stop_sequence, src.stop_id, src.route_id, src.direction_id,
             src.vehicle_id, src.trip_start_date, src.predicted_arrival_ts,
             src.predicted_departure_ts, src.schedule_relationship,
             @snapshot_ts, @snapshot_ts);
        SET @upsert_n = @@ROWCOUNT;

        -- (a.5) RESURRECTION
        --
        -- For every (trip_id, trip_start_date, stop_sequence) that staging
        -- contains a prediction for AND we previously emitted a
        -- 'suspect_cancelled' arrival for, delete the bogus arrival. MARTA
        -- has resumed publishing this trip; the next real disappearance will
        -- emit a correct arrival without UX_arrival_dedupe blocking it.
        --
        -- Scoped by trip_start_date so a NEW service day reusing the same
        -- trip_id can't accidentally erase yesterday's legitimate cancel.
        DELETE e
        FROM dbo.stop_arrival_event e
        WHERE e.derivation = 'suspect_cancelled'
          AND EXISTS (
            SELECT 1 FROM dbo.staging_predictions s
            WHERE s.trip_id         = e.trip_id
              AND s.stop_sequence   = e.stop_sequence
              AND s.trip_start_date = e.trip_start_date
          );
        SET @resurrected_n = @@ROWCOUNT;

        -- (b) snapshot the disappeared rows BEFORE we delete them
        DECLARE @disappeared TABLE (
            trip_id               varchar(16) NOT NULL,
            stop_sequence         smallint    NOT NULL,
            stop_id               varchar(16) NOT NULL,
            route_id              varchar(8)  NOT NULL,
            vehicle_id            varchar(16) NULL,
            trip_start_date       char(8)     NOT NULL,
            predicted_arrival_ts  datetime2(0) NULL,
            schedule_relationship tinyint      NULL
        );

        INSERT INTO @disappeared
            (trip_id, stop_sequence, stop_id, route_id, vehicle_id,
             trip_start_date, predicted_arrival_ts, schedule_relationship)
        SELECT trip_id, stop_sequence, stop_id, route_id, vehicle_id,
               trip_start_date, predicted_arrival_ts, schedule_relationship
        FROM dbo.trip_stop_prediction
        WHERE last_seen_ts < @snapshot_ts;

        SET @disappeared_n = @@ROWCOUNT;

        -- (c) derive arrivals
        IF @disappeared_n > 0 AND @prev_snapshot_ts IS NOT NULL
        BEGIN
            DECLARE @lo datetime2(0) = DATEADD(SECOND, -120, @prev_snapshot_ts);
            DECLARE @hi datetime2(0) = DATEADD(SECOND,  120, @snapshot_ts);

            ;WITH derived AS (
                SELECT
                    d.*,
                    CASE WHEN EXISTS (
                        SELECT 1 FROM dbo.trip_stop_prediction live
                        WHERE live.trip_id = d.trip_id
                          AND live.stop_sequence > d.stop_sequence
                          AND live.last_seen_ts = @snapshot_ts
                    ) THEN 1 ELSE 0 END AS trip_continued
                FROM @disappeared d
            )
            INSERT INTO dbo.stop_arrival_event
                (stop_id, route_id, direction_id, trip_id, trip_start_date, vehicle_id,
                 stop_sequence, observed_arrival_ts, last_predicted_arrival_ts,
                 derivation, confidence, derived_from_snapshot_ts)
            SELECT
                d.stop_id,
                d.route_id,
                t.direction_id,
                d.trip_id,
                d.trip_start_date,
                d.vehicle_id,
                d.stop_sequence,
                CASE
                    WHEN d.predicted_arrival_ts IS NULL                        THEN @snapshot_ts
                    WHEN d.predicted_arrival_ts BETWEEN @lo AND @hi            THEN d.predicted_arrival_ts
                    WHEN d.predicted_arrival_ts < @lo AND d.trip_continued = 1 THEN d.predicted_arrival_ts
                    WHEN d.predicted_arrival_ts < @lo AND d.trip_continued = 0 THEN @snapshot_ts
                    WHEN d.predicted_arrival_ts > @hi                          THEN @snapshot_ts
                    ELSE @snapshot_ts
                END,
                d.predicted_arrival_ts,
                CASE
                    WHEN d.predicted_arrival_ts > @hi AND d.trip_continued = 0 THEN 'suspect_cancelled'
                    ELSE 'dropped_from_upcoming'
                END,
                CASE
                    WHEN @gap_s <= 90
                         AND d.predicted_arrival_ts BETWEEN @prev_snapshot_ts AND @snapshot_ts
                         AND d.trip_continued = 1
                        THEN 'high'
                    WHEN @gap_s <= 90
                         AND (d.predicted_arrival_ts BETWEEN @prev_snapshot_ts AND @snapshot_ts
                              OR d.trip_continued = 1)
                        THEN 'medium'
                    ELSE 'low'
                END,
                @snapshot_ts
            FROM derived d
            LEFT JOIN dbo.trip t ON t.trip_id = d.trip_id
            WHERE ISNULL(d.schedule_relationship, 0) NOT IN (1, 2)
              AND NOT EXISTS (
                  SELECT 1 FROM dbo.stop_arrival_event e
                  WHERE e.trip_id         = d.trip_id
                    AND e.trip_start_date = d.trip_start_date
                    AND e.stop_sequence   = d.stop_sequence
              );
            SET @derived_n = @@ROWCOUNT;
        END

        DELETE FROM dbo.trip_stop_prediction WHERE last_seen_ts < @snapshot_ts;
    END

    TRUNCATE TABLE dbo.staging_predictions;

    INSERT INTO dbo.snapshot
        (snapshot_ts, feed_ts, vehicle_entity_n, trip_entity_n,
         poll_duration_ms, upsert_n, disappeared_n, derived_n,
         resurrected_n, skipped_reason)
    VALUES
        (@snapshot_ts, @feed_ts, @vehicle_entity_n, @trip_entity_n,
         @poll_duration_ms, @upsert_n, @disappeared_n, @derived_n,
         @resurrected_n, @skipped_reason);

    SELECT
        @snapshot_ts      AS snapshot_ts,
        @prev_snapshot_ts AS prev_snapshot_ts,
        @gap_s            AS gap_s,
        @upsert_n         AS upsert_n,
        @disappeared_n    AS disappeared_n,
        @derived_n        AS derived_n,
        @resurrected_n    AS resurrected_n,
        @skipped_reason   AS skipped_reason;
END
GO
