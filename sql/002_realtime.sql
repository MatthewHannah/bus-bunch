-- 002_realtime.sql
-- ----------------------------------------------------------------------------
-- Realtime tables for MARTA GTFS-realtime ingestion.
--
-- Design model (v3, 2026-04):
--
--   trip_stop_prediction is now LIVE STATE, not an append log:
--     * one row per (trip_id, stop_sequence) currently being predicted
--     * MERGE'd in place each poll
--     * a row that disappears from the feed = the bus arrived; an event is
--       emitted to stop_arrival_event and the row is deleted
--     * total size is bounded by the size of MARTA's prediction set (~17K
--       rows) — does not grow with time
--
--   staging_predictions is a scratch table SqlBulkCopy'd from C# each poll
--     * truncated at the end of each poll by sp_ProcessPredictionPoll
--     * exists as a permanent table only because SqlBulkCopy across a
--       transaction needs a real (non-temp) target
--
--   snapshot is the heartbeat fact: one row per successful poll, kept
--     forever, with diagnostic counters for upserts/disappearances/derives
--     plus a skipped_reason for outage-skipped polls
--
--   vehicle_position_snapshot is append-per-poll, capped at 7 days by
--     sp_PruneVehiclePositions (see 005_*.sql)
--
--   stop_arrival_event is the derived fact, kept forever
--
-- Conventions:
--   * datetime2(0) UTC everywhere
--   * varchar IDs sized for MARTA's actual widths
--   * trip_start_date is NOT NULL DEFAULT '' so UNIQUE constraints work
--     correctly (SQL Server's UNIQUE allows only one NULL, which would
--     silently break dedup)
--   * no FK from vehicle_position_snapshot to snapshot (snapshot rows are
--     immortal so it would never trigger; FK adds insert overhead for no
--     benefit)
-- ----------------------------------------------------------------------------

IF OBJECT_ID('dbo.stop_arrival_event', 'U')        IS NOT NULL DROP TABLE dbo.stop_arrival_event;
IF OBJECT_ID('dbo.staging_predictions', 'U')       IS NOT NULL DROP TABLE dbo.staging_predictions;
IF OBJECT_ID('dbo.trip_stop_prediction', 'U')      IS NOT NULL DROP TABLE dbo.trip_stop_prediction;
IF OBJECT_ID('dbo.vehicle_position_snapshot', 'U') IS NOT NULL DROP TABLE dbo.vehicle_position_snapshot;
IF OBJECT_ID('dbo.snapshot', 'U')                  IS NOT NULL DROP TABLE dbo.snapshot;
GO

-- ---------------------------------------------------------------------------
-- snapshot: heartbeat. One row per successful poll. Kept forever (tiny).
-- ---------------------------------------------------------------------------
CREATE TABLE dbo.snapshot
(
    snapshot_ts        datetime2(0) NOT NULL,
    feed_ts            datetime2(0) NOT NULL,
    vehicle_entity_n   int          NULL,    -- entities in the vehicle feed
    trip_entity_n      int          NULL,    -- entities in the trip-update feed
    poll_duration_ms   int          NULL,
    upsert_n           int          NULL,    -- predictions inserted+updated
    disappeared_n      int          NULL,    -- predictions dropped from feed this poll
    derived_n          int          NULL,    -- arrival events emitted this poll
    skipped_reason     varchar(32)  NULL,    -- 'gap_too_large' / 'mass_dropout' / NULL
    CONSTRAINT PK_snapshot PRIMARY KEY CLUSTERED (snapshot_ts)
);
GO

-- ---------------------------------------------------------------------------
-- vehicle_position_snapshot: bus GPS, append-per-poll, 7-day rolling.
-- ---------------------------------------------------------------------------
CREATE TABLE dbo.vehicle_position_snapshot
(
    snapshot_ts    datetime2(0) NOT NULL,
    vehicle_id     varchar(16)  NOT NULL,
    route_id       varchar(8)   NULL,
    direction_id   tinyint      NULL,
    trip_id        varchar(16)  NULL,
    latitude       decimal(9,6) NULL,
    longitude      decimal(9,6) NULL,
    bearing        smallint     NULL,
    speed_mps      decimal(5,2) NULL,
    veh_ts         datetime2(0) NULL,
    CONSTRAINT PK_vehicle_position_snapshot
        PRIMARY KEY CLUSTERED (snapshot_ts, vehicle_id)
)
WITH (DATA_COMPRESSION = PAGE);
GO

-- (No NCI on vehicle_id. At 7-day retention ~2.3M rows, the NCI would cost
--  ~400MB; "trail of bus X" queries can do a clustered scan in a few hundred
--  ms which is fine for ad-hoc analysis. Re-add if it becomes a hot path.)

-- ---------------------------------------------------------------------------
-- trip_stop_prediction: LIVE STATE — one row per currently-predicted
-- (trip_id, stop_sequence). MERGE'd in place each poll.
-- ---------------------------------------------------------------------------
CREATE TABLE dbo.trip_stop_prediction
(
    trip_id                varchar(16)  NOT NULL,
    stop_sequence          smallint     NOT NULL,
    stop_id                varchar(16)  NOT NULL,
    route_id               varchar(8)   NOT NULL,
    direction_id           tinyint      NULL,
    vehicle_id             varchar(16)  NULL,
    trip_start_date        char(8)      NOT NULL DEFAULT '',
    predicted_arrival_ts   datetime2(0) NULL,
    predicted_departure_ts datetime2(0) NULL,
    schedule_relationship  tinyint      NULL,   -- 0=SCHEDULED, 1=SKIPPED, 2=NO_DATA, 3=UNSCHEDULED
    first_seen_ts          datetime2(0) NOT NULL,
    last_seen_ts           datetime2(0) NOT NULL,
    CONSTRAINT PK_trip_stop_prediction
        PRIMARY KEY CLUSTERED (trip_id, stop_sequence)
);
GO

-- ---------------------------------------------------------------------------
-- staging_predictions: scratch target for SqlBulkCopy from C#. Truncated
-- by sp_ProcessPredictionPoll at the end of each successful poll.
-- ---------------------------------------------------------------------------
CREATE TABLE dbo.staging_predictions
(
    trip_id                varchar(16)  NOT NULL,
    stop_sequence          smallint     NOT NULL,
    stop_id                varchar(16)  NOT NULL,
    route_id               varchar(8)   NOT NULL,
    direction_id           tinyint      NULL,
    vehicle_id             varchar(16)  NULL,
    trip_start_date        char(8)      NOT NULL DEFAULT '',
    predicted_arrival_ts   datetime2(0) NULL,
    predicted_departure_ts datetime2(0) NULL,
    schedule_relationship  tinyint      NULL,
    CONSTRAINT PK_staging_predictions
        PRIMARY KEY CLUSTERED (trip_id, stop_sequence)
);
GO

-- ---------------------------------------------------------------------------
-- stop_arrival_event: derived FACT. Kept forever. Headway/bunching views
-- read from this.
-- ---------------------------------------------------------------------------
CREATE TABLE dbo.stop_arrival_event
(
    event_id                  bigint       IDENTITY(1,1) NOT NULL,
    stop_id                   varchar(16)  NOT NULL,
    route_id                  varchar(8)   NOT NULL,
    direction_id              tinyint      NULL,
    trip_id                   varchar(16)  NOT NULL,
    trip_start_date           char(8)      NOT NULL DEFAULT '',
    vehicle_id                varchar(16)  NULL,
    stop_sequence             smallint     NOT NULL,
    observed_arrival_ts       datetime2(0) NOT NULL,
    last_predicted_arrival_ts datetime2(0) NULL,
    derivation                varchar(32)  NOT NULL,   -- 'dropped_from_upcoming' | 'suspect_cancelled'
    confidence                varchar(8)   NOT NULL,   -- 'high' | 'medium' | 'low'
    derived_from_snapshot_ts  datetime2(0) NOT NULL,
    derived_at                datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_stop_arrival_event PRIMARY KEY NONCLUSTERED (event_id),
    CONSTRAINT UX_arrival_dedupe
        UNIQUE (trip_id, trip_start_date, stop_sequence)
)
WITH (DATA_COMPRESSION = PAGE);
GO

CREATE CLUSTERED INDEX IX_arrival_stop_time
    ON dbo.stop_arrival_event (stop_id, observed_arrival_ts);
GO

CREATE NONCLUSTERED INDEX IX_arrival_route_time
    ON dbo.stop_arrival_event (route_id, direction_id, observed_arrival_ts)
    INCLUDE (stop_id, vehicle_id, trip_id);
GO
