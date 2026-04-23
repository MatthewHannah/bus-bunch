using BusBunch.Functions.Services;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Extensions.Logging;

namespace BusBunch.Functions.Functions;

/// <summary>
/// Hourly prune of dbo.vehicle_position_snapshot rows older than
/// Retention:VehiclePositionDays (default 7).
///
/// In the v3 design realtime predictions are bounded by the upsert pattern
/// in sp_ProcessPredictionPoll, so this is the only background cleanup.
/// Also exposed as an HTTP endpoint for manual cleanup.
/// </summary>
public class PruneVehiclePositionsFunction
{
    private readonly VehiclePositionPruner _pruner;
    private readonly ILogger<PruneVehiclePositionsFunction> _logger;

    public PruneVehiclePositionsFunction(VehiclePositionPruner pruner, ILogger<PruneVehiclePositionsFunction> logger)
    {
        _pruner = pruner;
        _logger = logger;
    }

    [Function("PruneVehiclePositions_Timer")]
    public async Task RunTimer(
        [TimerTrigger("0 17 * * * *")] TimerInfo timer,
        CancellationToken ct)
    {
        _logger.LogInformation("Hourly vehicle-position prune triggered");
        await _pruner.PruneAsync(ct);
    }

    [Function("PruneVehiclePositions_Http")]
    public async Task<IActionResult> RunHttp(
        [HttpTrigger(AuthorizationLevel.Function, "post", Route = "prune-vehicle-positions")]
        HttpRequest req,
        CancellationToken ct)
    {
        _logger.LogInformation("On-demand vehicle-position prune triggered");
        var result = await _pruner.PruneAsync(ct);
        return new OkObjectResult(result);
    }
}
