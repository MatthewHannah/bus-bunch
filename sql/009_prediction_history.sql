-- 009_prediction_history.sql
-- ----------------------------------------------------------------------------
-- Track every ETA prediction MARTA publishes for a configurable subset of
-- routes, so we can measure stop-arrival prediction accuracy as a function of
-- horizon, route, time-of-day, etc.
--
-- Architecture (intentionally decoupled from the v3 derive engine):
--
--   * dbo.tracked_route — one row per route_id we care about. Empty by
--     default; populate manually with INSERTs to start recording.
--
--   * dbo.prediction_history — append-only fact. Populated by
--     dbo.sp_RecordPredictionHistory (010) which is called by SnapshotWriter
--     BEFORE dbo.sp_ProcessPredictionPoll runs. Recording is unconditional:
--     it captures predictions on `gap_too_large`, `gap_no_data`, and
--     `mass_dropout` polls too — that's MARTA's actual behavior and is
--     exactly what we want to measure.
--
--   * dbo.sp_PrunePredictionHistory (011) — keeps the table under a
--     configurable storage cap by deleting the oldest hour at a time.
--
-- Indexing:
--   * Clustered PK leads with snapshot_ts so the storage-target pruner can
--     do a cheap clustered range delete (mirrors vehicle_position_snapshot).
--   * NCI on (trip_id, trip_start_date, stop_sequence, snapshot_ts) for the
--     analytical join into stop_arrival_event.
--   * PAGE compression — predictions repeat heavily across polls (same
--     trip + stop, predicted_arrival_ts barely drifts), so compression is a
--     big win.
--
-- Sizing (rough): ~150 live predictions per tracked route × 2880 polls/day
-- ≈ 430K rows/day per route. With page compression: ~25–35 MB/route/day.
-- The NCI roughly doubles that. Default prune target of 600 MB ≈ a few days
-- with 5 routes tracked, weeks with one route.
-- ----------------------------------------------------------------------------

IF OBJECT_ID('dbo.prediction_history', 'U') IS NOT NULL DROP TABLE dbo.prediction_history;
IF OBJECT_ID('dbo.tracked_route', 'U')      IS NOT NULL DROP TABLE dbo.tracked_route;
GO

-- ---------------------------------------------------------------------------
-- tracked_route: which realtime route_ids to record predictions for.
-- Populate manually:
--   INSERT INTO dbo.tracked_route (route_id) VALUES ('21'), ('22');
-- Adding/removing a route takes effect on the next poll.
-- ---------------------------------------------------------------------------
CREATE TABLE dbo.tracked_route
(
    route_id   varchar(8)   NOT NULL,
    added_at   datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    note       nvarchar(256) NULL,
    CONSTRAINT PK_tracked_route PRIMARY KEY CLUSTERED (route_id)
);
GO

-- ---------------------------------------------------------------------------
-- prediction_history: append-only per-poll snapshot of MARTA's predictions
-- for tracked routes. Joins to stop_arrival_event on
-- (trip_id, trip_start_date, stop_sequence) for accuracy analysis.
-- ---------------------------------------------------------------------------
CREATE TABLE dbo.prediction_history
(
    snapshot_ts            datetime2(0) NOT NULL,
    route_id               varchar(8)   NOT NULL,
    trip_id                varchar(16)  NOT NULL,
    trip_start_date        char(8)      NOT NULL,
    stop_sequence          smallint     NOT NULL,
    stop_id                varchar(16)  NOT NULL,
    direction_id           tinyint      NULL,
    vehicle_id             varchar(16)  NULL,
    predicted_arrival_ts   datetime2(0) NULL,
    schedule_relationship  tinyint      NULL,
    CONSTRAINT PK_prediction_history
        PRIMARY KEY CLUSTERED
            (snapshot_ts, route_id, trip_id, trip_start_date, stop_sequence)
)
WITH (DATA_COMPRESSION = PAGE);
GO

-- For joining to stop_arrival_event by (trip_id, trip_start_date, stop_sequence).
CREATE NONCLUSTERED INDEX IX_prediction_history_trip
    ON dbo.prediction_history (trip_id, trip_start_date, stop_sequence, snapshot_ts)
    INCLUDE (route_id, stop_id, direction_id, vehicle_id,
             predicted_arrival_ts, schedule_relationship)
    WITH (DATA_COMPRESSION = PAGE);
GO
