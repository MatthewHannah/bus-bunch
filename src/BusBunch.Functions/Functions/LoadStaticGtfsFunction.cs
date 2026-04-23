using BusBunch.Functions.Services;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Azure.Functions.Worker.Http;
using Microsoft.Extensions.Logging;

namespace BusBunch.Functions.Functions;

/// <summary>
/// Loads the agency's static GTFS bundle into the dim tables.
///
/// Two triggers point at the same logic:
///   * Timer  - weekly Mondays 09:00 UTC (~05:00 ET)
///   * Http   - on-demand POST /api/load-static-gtfs (admin-keyed) so we can
///              kick off a fresh load right after deploy without waiting for
///              the timer to fire
/// </summary>
public class LoadStaticGtfsFunction
{
    private readonly StaticGtfsLoader _loader;
    private readonly ILogger<LoadStaticGtfsFunction> _logger;

    public LoadStaticGtfsFunction(StaticGtfsLoader loader, ILogger<LoadStaticGtfsFunction> logger)
    {
        _loader = loader;
        _logger = logger;
    }

    [Function("LoadStaticGtfs_Timer")]
    public async Task RunTimer(
        [TimerTrigger("0 0 9 * * 1")] TimerInfo timer,
        CancellationToken ct)
    {
        _logger.LogInformation("Weekly static-GTFS load triggered");
        await _loader.LoadAsync(ct);
    }

    [Function("LoadStaticGtfs_Http")]
    public async Task<IActionResult> RunHttp(
        [HttpTrigger(AuthorizationLevel.Function, "post", Route = "load-static-gtfs")]
        HttpRequest req,
        CancellationToken ct)
    {
        _logger.LogInformation("On-demand static-GTFS load triggered");
        var result = await _loader.LoadAsync(ct);
        return new OkObjectResult(result);
    }
}
