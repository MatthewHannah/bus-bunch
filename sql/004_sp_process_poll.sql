-- 004_sp_process_poll.sql
-- ----------------------------------------------------------------------------
-- dbo.sp_ProcessPredictionPoll
--
-- The per-poll engine. Called once per poll by SnapshotWriter, *after* it
-- has SqlBulkCopy'd vehicle positions to dbo.vehicle_position_snapshot and
-- the parsed predictions to dbo.staging_predictions.
--
-- Inside one transaction (the caller's), this proc:
--   1. computes prev_snapshot_ts and gap_s
--   2. decides whether to skip the derive (gap_too_large / mass_dropout)
--   3. if not skipping:
--        a. MERGEs staging into trip_stop_prediction (live state)
--        b. derives arrivals for rows that disappeared this poll
--        c. DELETEs the disappeared rows from trip_stop_prediction
--   4. truncates staging_predictions
--   5. INSERTs the snapshot heartbeat with all counters
--
-- Skip semantics: when we skip, we *do not* MERGE, *do not* delete, *do not*
-- emit events. The live table is untouched. The next healthy poll will
-- naturally refresh most rows; anything still missing then is treated as a
-- normal disappearance. This avoids emitting bogus arrivals during a feed
-- glitch at the cost of slightly stale last_seen_ts for one poll.
--
-- Derive decision tree (per disappeared row), unchanged from the v2 logic:
--
--   Skip rows with prev.schedule_relationship IN (1=SKIPPED, 2=NO_DATA).
--
--   Let pa = prev.predicted_arrival_ts.
--   Let trip_continued = exists same trip_id with higher stop_sequence in
--                        the post-MERGE live table.
--   Let lo = prev_ts - 120s, hi = this_ts + 120s.
--
--   observed_arrival_ts:
--     pa NULL                                  -> @snapshot_ts
--     pa BETWEEN lo AND hi                     -> pa
--     pa < lo AND trip_continued = 1           -> pa  (corroborated stale)
--     pa < lo AND trip_continued = 0           -> @snapshot_ts
--     pa > hi                                  -> @snapshot_ts (suspect cancel)
--
--   derivation:
--     pa > hi AND trip_continued = 0           -> 'suspect_cancelled'
--     otherwise                                -> 'dropped_from_upcoming'
--
--   confidence:
--     high   = gap_s<=90 AND pa BETWEEN [@prev,@this] AND trip_continued
--     medium = gap_s<=90 AND (pa BETWEEN [@prev,@this] OR trip_continued)
--     low    = otherwise
-- ----------------------------------------------------------------------------

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
          AND skipped_reason IS NULL  -- only "good" prior snapshots count as the diff baseline
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

        -- delete the disappeared rows; live table now matches @snapshot_ts exactly
        DELETE FROM dbo.trip_stop_prediction WHERE last_seen_ts < @snapshot_ts;
    END

    -- Always clear staging after a poll, even when skipped. Otherwise the
    -- next poll's SqlBulkCopy into staging_predictions will hit PK violations
    -- on (trip_id, stop_sequence) because the old rows are still present.
    TRUNCATE TABLE dbo.staging_predictions;

    -- (5) heartbeat
    INSERT INTO dbo.snapshot
        (snapshot_ts, feed_ts, vehicle_entity_n, trip_entity_n,
         poll_duration_ms, upsert_n, disappeared_n, derived_n, skipped_reason)
    VALUES
        (@snapshot_ts, @feed_ts, @vehicle_entity_n, @trip_entity_n,
         @poll_duration_ms, @upsert_n, @disappeared_n, @derived_n, @skipped_reason);

    SELECT
        @snapshot_ts      AS snapshot_ts,
        @prev_snapshot_ts AS prev_snapshot_ts,
        @gap_s            AS gap_s,
        @upsert_n         AS upsert_n,
        @disappeared_n    AS disappeared_n,
        @derived_n        AS derived_n,
        @skipped_reason   AS skipped_reason;
END
GO
