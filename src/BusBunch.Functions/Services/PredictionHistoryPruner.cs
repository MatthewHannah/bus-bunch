using System.Data;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Logging;

namespace BusBunch.Functions.Services;

/// <summary>
/// Calls dbo.sp_PrunePredictionHistory with the configured storage target.
///
/// Unlike VehiclePositionPruner this is storage-target-based, not
/// time-based: the goal of prediction_history is "track as long as we can
/// before storage runs out", so the pruner caps total used MB and lets
/// effective retention float with the recording rate (which depends on how
/// many routes are in dbo.tracked_route).
///
/// vw_prediction_history_window exposes oldest_snapshot_ts and used_mb so
/// callers can see what retention the cap actually buys them.
/// </summary>
public class PredictionHistoryPruner
{
    private readonly SqlOptions _sql;
    private readonly RetentionOptions _options;
    private readonly ILogger<PredictionHistoryPruner> _logger;

    public PredictionHistoryPruner(SqlOptions sql, RetentionOptions options, ILogger<PredictionHistoryPruner> logger)
    {
        _sql = sql;
        _options = options;
        _logger = logger;
    }

    public async Task<PredictionPruneResult> PruneAsync(CancellationToken ct)
    {
        await using var conn = new SqlConnection(_sql.ConnectionString);
        await conn.OpenAsync(ct);
        await using var cmd = new SqlCommand("dbo.sp_PrunePredictionHistory", conn)
        {
            CommandType = CommandType.StoredProcedure,
            CommandTimeout = 600,
        };
        cmd.Parameters.Add("@target_mb", SqlDbType.Int).Value = _options.PredictionHistoryTargetMb;

        await using var reader = await cmd.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct))
            throw new InvalidOperationException("sp_PrunePredictionHistory returned no rows");

        var result = new PredictionPruneResult(
            DeletedRows: reader.GetInt64(0),
            FinalMb:     reader.IsDBNull(1) ? null : reader.GetInt32(1),
            TargetMb:    reader.GetInt32(2),
            Loops:       reader.GetInt32(3));

        _logger.LogInformation(
            "Pruned prediction_history: target={Target}MB, final={Final}MB, deleted={Deleted}, loops={Loops}",
            result.TargetMb, result.FinalMb, result.DeletedRows, result.Loops);
        return result;
    }
}

public sealed record PredictionPruneResult(long DeletedRows, int? FinalMb, int TargetMb, int Loops);
