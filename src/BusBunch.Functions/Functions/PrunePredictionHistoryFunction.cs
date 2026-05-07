using BusBunch.Functions.Services;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Extensions.Logging;

namespace BusBunch.Functions.Functions;

/// <summary>
/// Hourly storage-target prune of dbo.prediction_history. Deletes the
/// oldest data first until the table fits under
/// Retention:PredictionHistoryTargetMb.
///
/// Scheduled at minute 32 (offset from the vehicle-position pruner at
/// minute 17) so the two pruners don't compete for log throughput on the
/// Basic 5 DTU tier. Also exposed as an HTTP endpoint for manual cleanup.
/// </summary>
public class PrunePredictionHistoryFunction
{
    private readonly PredictionHistoryPruner _pruner;
    private readonly ILogger<PrunePredictionHistoryFunction> _logger;

    public PrunePredictionHistoryFunction(
        PredictionHistoryPruner pruner,
        ILogger<PrunePredictionHistoryFunction> logger)
    {
        _pruner = pruner;
        _logger = logger;
    }

    [Function("PrunePredictionHistory_Timer")]
    public async Task RunTimer(
        [TimerTrigger("0 32 * * * *")] TimerInfo timer,
        CancellationToken ct)
    {
        _logger.LogInformation("Hourly prediction-history prune triggered");
        await _pruner.PruneAsync(ct);
    }

    [Function("PrunePredictionHistory_Http")]
    public async Task<IActionResult> RunHttp(
        [HttpTrigger(AuthorizationLevel.Function, "post", Route = "prune-prediction-history")]
        HttpRequest req,
        CancellationToken ct)
    {
        _logger.LogInformation("On-demand prediction-history prune triggered");
        var result = await _pruner.PruneAsync(ct);
        return new OkObjectResult(result);
    }
}
