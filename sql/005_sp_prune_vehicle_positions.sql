-- 005_sp_prune_vehicle_positions.sql
-- ----------------------------------------------------------------------------
-- dbo.sp_PruneVehiclePositions @cutoff_ts
--
-- The only thing that needs periodic pruning in the v3 design is
-- vehicle_position_snapshot (append-per-poll). The realtime predictions
-- are bounded by sp_ProcessPredictionPoll's MERGE-then-DELETE pattern;
-- snapshot heartbeats and stop_arrival_event are kept forever.
--
-- Deletes in 5000-row batches to keep transaction log small (Basic 5 DTU
-- is log-throughput bound). Returns total rows deleted.
-- ----------------------------------------------------------------------------

IF OBJECT_ID('dbo.sp_PruneVehiclePositions', 'P') IS NOT NULL
    DROP PROCEDURE dbo.sp_PruneVehiclePositions;
GO

CREATE PROCEDURE dbo.sp_PruneVehiclePositions
    @cutoff_ts datetime2(0)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @deleted int = 0;
    DECLARE @batch   int = 1;

    WHILE @batch > 0
    BEGIN
        DELETE TOP (5000) FROM dbo.vehicle_position_snapshot
            WHERE snapshot_ts < @cutoff_ts;
        SET @batch = @@ROWCOUNT;
        SET @deleted += @batch;
    END

    SELECT @deleted AS deleted_positions, @cutoff_ts AS cutoff_ts;
END
GO
