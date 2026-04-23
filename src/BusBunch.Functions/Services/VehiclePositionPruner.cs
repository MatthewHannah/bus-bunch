using System.Data;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Logging;

namespace BusBunch.Functions.Services;

/// <summary>
/// Calls dbo.sp_PruneVehiclePositions with cutoff = (now - retentionDays).
/// In the v3 design the realtime predictions are bounded by the upsert
/// pattern in sp_ProcessPredictionPoll, so this is the only periodic
/// cleanup that's needed.
/// </summary>
public class VehiclePositionPruner
{
    private readonly SqlOptions _sql;
    private readonly RetentionOptions _options;
    private readonly ILogger<VehiclePositionPruner> _logger;

    public VehiclePositionPruner(SqlOptions sql, RetentionOptions options, ILogger<VehiclePositionPruner> logger)
    {
        _sql = sql;
        _options = options;
        _logger = logger;
    }

    public async Task<PruneResult> PruneAsync(CancellationToken ct)
    {
        var cutoff = DateTime.UtcNow.AddDays(-_options.VehiclePositionDays);
        cutoff = new DateTime(cutoff.Year, cutoff.Month, cutoff.Day,
                              cutoff.Hour, cutoff.Minute, cutoff.Second,
                              DateTimeKind.Utc);

        await using var conn = new SqlConnection(_sql.ConnectionString);
        await conn.OpenAsync(ct);
        await using var cmd = new SqlCommand("dbo.sp_PruneVehiclePositions", conn)
        {
            CommandType = CommandType.StoredProcedure,
            CommandTimeout = 600,
        };
        cmd.Parameters.Add("@cutoff_ts", SqlDbType.DateTime2, 0).Value = cutoff;

        await using var reader = await cmd.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct))
            throw new InvalidOperationException("sp_PruneVehiclePositions returned no rows");

        var result = new PruneResult(
            DeletedPositions: reader.GetInt32(0),
            CutoffTs:         reader.GetDateTime(1));

        _logger.LogInformation(
            "Pruned vehicle positions: cutoff={Cutoff:o}, {N} rows deleted",
            result.CutoffTs, result.DeletedPositions);
        return result;
    }
}

public sealed class RetentionOptions
{
    public int VehiclePositionDays { get; init; } = 7;
}

public sealed record PruneResult(int DeletedPositions, DateTime CutoffTs);
