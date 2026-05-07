-- 011_sp_prune_prediction_history.sql
-- ----------------------------------------------------------------------------
-- dbo.sp_PrunePredictionHistory @target_mb
--
-- Storage-target pruner for dbo.prediction_history. Called hourly by
-- PrunePredictionHistoryFunction. Walks the table from oldest to newest,
-- deleting one hour at a time in 5000-row batches, until used size is at or
-- below @target_mb.
--
-- Why storage-based instead of time-based: the user's stated goal is "track
-- as long as we can before storage runs out". Time-based ("keep N days")
-- would either over- or under-spend depending on how many routes are
-- tracked. Storage-based gives a hard cap with whatever retention the
-- recording rate allows.
--
-- vw_prediction_history_window (012) exposes oldest_snapshot_ts and used_mb
-- so callers can see current effective retention.
--
-- The DMV read requires VIEW DATABASE STATE; granted to PUBLIC at the
-- bottom of this file so the Function App's MI (db_ddladmin) can run it.
-- ----------------------------------------------------------------------------

IF OBJECT_ID('dbo.sp_PrunePredictionHistory', 'P') IS NOT NULL
    DROP PROCEDURE dbo.sp_PrunePredictionHistory;
GO

CREATE PROCEDURE dbo.sp_PrunePredictionHistory
    @target_mb int
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @deleted     bigint = 0;
    DECLARE @loops       int    = 0;
    DECLARE @current_mb  int    = NULL;
    DECLARE @oldest_ts   datetime2(0);
    DECLARE @cutoff_ts   datetime2(0);
    DECLARE @batch       int;

    -- Safety cap: even at 1 hour deleted per loop, 240 covers 10 days. If
    -- we'd need more, something else is wrong (caller can re-invoke).
    WHILE @loops < 240
    BEGIN
        SET @loops += 1;

        -- Sum across all indexes (clustered + NCI). index_id IN (0,1) would
        -- miss the NCI; using ALL indexes gives the true on-disk cost.
        SELECT @current_mb = SUM(used_page_count) * 8 / 1024
        FROM sys.dm_db_partition_stats
        WHERE object_id = OBJECT_ID('dbo.prediction_history');

        IF @current_mb IS NULL OR @current_mb <= @target_mb BREAK;

        SELECT @oldest_ts = MIN(snapshot_ts) FROM dbo.prediction_history;
        IF @oldest_ts IS NULL BREAK;

        -- Round @oldest_ts up to the next hour boundary; delete everything
        -- strictly before that. Always deletes at least one hour-worth.
        SET @cutoff_ts = DATEADD(HOUR, DATEDIFF(HOUR, 0, @oldest_ts) + 1, 0);

        SET @batch = 1;
        WHILE @batch > 0
        BEGIN
            DELETE TOP (5000) FROM dbo.prediction_history
                WHERE snapshot_ts < @cutoff_ts;
            SET @batch = @@ROWCOUNT;
            SET @deleted += @batch;
        END
    END

    SELECT
        @deleted    AS deleted_rows,
        @current_mb AS final_mb,
        @target_mb  AS target_mb,
        @loops      AS loops;
END
GO

-- The MI is a db_ddladmin / db_datareader / db_datawriter user; none of
-- those roles grant VIEW DATABASE STATE, which sys.dm_db_partition_stats
-- requires. Grant to PUBLIC so any database principal can read DMVs.
GRANT VIEW DATABASE STATE TO PUBLIC;
GO
