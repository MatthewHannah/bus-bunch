-- 003_static_gtfs.sql
-- ----------------------------------------------------------------------------
-- Dimension tables loaded weekly from MARTA's static GTFS bundle.
--
-- ⚠️ Static vs realtime route_id mismatch:
--   The static feed uses internal route_ids like '26916'/'26917'.
--   The realtime feed uses public route numbers like '21'/'22'.
--   trip_id IS consistent across both feeds.
--   When joining static schedule data to realtime data, JOIN VIA trip_id,
--   not route_id. To group by public route, go via route.route_short_name.
-- ----------------------------------------------------------------------------

IF OBJECT_ID('dbo.scheduled_stop_time', 'U') IS NOT NULL DROP TABLE dbo.scheduled_stop_time;
IF OBJECT_ID('dbo.trip', 'U')                IS NOT NULL DROP TABLE dbo.trip;
IF OBJECT_ID('dbo.route', 'U')               IS NOT NULL DROP TABLE dbo.route;
IF OBJECT_ID('dbo.stop', 'U')                IS NOT NULL DROP TABLE dbo.stop;
IF OBJECT_ID('dbo.gtfs_static_version', 'U') IS NOT NULL DROP TABLE dbo.gtfs_static_version;
GO

CREATE TABLE dbo.gtfs_static_version
(
    loaded_at       datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    feed_start_date char(8)      NULL,
    feed_end_date   char(8)      NULL,
    source_etag     varchar(64)  NULL,
    CONSTRAINT PK_gtfs_static_version PRIMARY KEY CLUSTERED (loaded_at)
);
GO

CREATE TABLE dbo.stop
(
    stop_id        varchar(16)   NOT NULL,
    stop_code      varchar(16)   NULL,
    stop_name      nvarchar(128) NOT NULL,
    stop_lat       decimal(9,6)  NULL,
    stop_lon       decimal(9,6)  NULL,
    location_type  tinyint       NULL,
    parent_station varchar(16)   NULL,
    CONSTRAINT PK_stop PRIMARY KEY CLUSTERED (stop_id)
);
GO

CREATE TABLE dbo.route
(
    route_id         varchar(8)    NOT NULL,
    route_short_name varchar(16)   NULL,
    route_long_name  nvarchar(128) NULL,
    route_type       tinyint       NULL,    -- 3 = bus, 1 = subway, 2 = rail
    CONSTRAINT PK_route PRIMARY KEY CLUSTERED (route_id)
);
GO

CREATE TABLE dbo.trip
(
    trip_id       varchar(16)   NOT NULL,
    route_id      varchar(8)    NOT NULL,
    service_id    varchar(32)   NULL,
    direction_id  tinyint       NULL,
    trip_headsign nvarchar(128) NULL,
    block_id      varchar(32)   NULL,
    shape_id      varchar(32)   NULL,
    CONSTRAINT PK_trip PRIMARY KEY CLUSTERED (trip_id),
    CONSTRAINT FK_trip_route FOREIGN KEY (route_id) REFERENCES dbo.route (route_id)
);
GO

CREATE NONCLUSTERED INDEX IX_trip_route_dir ON dbo.trip (route_id, direction_id);
GO

CREATE TABLE dbo.scheduled_stop_time
(
    trip_id        varchar(16) NOT NULL,
    stop_sequence  smallint    NOT NULL,
    stop_id        varchar(16) NOT NULL,
    arrival_time   int         NULL,    -- seconds since midnight, may exceed 86400
    departure_time int         NULL,
    pickup_type    tinyint     NULL,
    drop_off_type  tinyint     NULL,
    CONSTRAINT PK_scheduled_stop_time PRIMARY KEY CLUSTERED (trip_id, stop_sequence),
    CONSTRAINT FK_sst_trip FOREIGN KEY (trip_id) REFERENCES dbo.trip (trip_id)
)
WITH (DATA_COMPRESSION = PAGE);
GO

CREATE NONCLUSTERED INDEX IX_sst_stop_time
    ON dbo.scheduled_stop_time (stop_id, arrival_time)
    INCLUDE (trip_id, stop_sequence, departure_time);
GO
