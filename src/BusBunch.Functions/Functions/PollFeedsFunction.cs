using System.Diagnostics;
using BusBunch.Functions.Services;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Extensions.Logging;

namespace BusBunch.Functions.Functions;

/// <summary>
/// Timer-triggered Function: polls MARTA every 30s, writes a snapshot,
/// and derives arrival events inline.
///
/// Adaptive cadence: outside MARTA service hours (~05:00 - 01:00 ET) we
/// only do real work every 5 minutes to save executions.
/// </summary>
public class PollFeedsFunction
{
    private static readonly TimeZoneInfo Eastern =
        TimeZoneInfo.FindSystemTimeZoneById(
            OperatingSystem.IsWindows() ? "Eastern Standard Time" : "America/New_York");

    private readonly GtfsRealtimeClient _feed;
    private readonly SnapshotWriter _writer;
    private readonly ILogger<PollFeedsFunction> _logger;

    public PollFeedsFunction(
        GtfsRealtimeClient feed,
        SnapshotWriter writer,
        ILogger<PollFeedsFunction> logger)
    {
        _feed = feed;
        _writer = writer;
        _logger = logger;
    }

    [Function(nameof(PollFeedsFunction))]
    public async Task Run(
        [TimerTrigger("*/30 * * * * *")] TimerInfo timer,
        CancellationToken ct)
    {
        var nowUtc = DateTime.UtcNow;

        if (!ShouldPollNow(nowUtc))
        {
            _logger.LogDebug("Outside service window, skipping ({Now:HH:mm} ET)",
                TimeZoneInfo.ConvertTimeFromUtc(nowUtc, Eastern));
            return;
        }

        var sw = Stopwatch.StartNew();
        try
        {
            var vehTask  = _feed.FetchVehiclePositionsAsync(ct);
            var tripTask = _feed.FetchTripUpdatesAsync(ct);
            await Task.WhenAll(vehTask, tripTask);

            await _writer.WriteAsync(nowUtc, vehTask.Result, tripTask.Result,
                (int)sw.ElapsedMilliseconds, ct);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Polling failed after {Ms}ms", sw.ElapsedMilliseconds);
            throw;
        }
    }

    /// <summary>
    /// Service window: 05:00 - 01:00 Eastern. Polling is disabled outside this window.
    /// </summary>
    internal static bool ShouldPollNow(DateTime utcNow)
    {
        var et = TimeZoneInfo.ConvertTimeFromUtc(utcNow, Eastern);
        var hour = et.Hour;
        return hour >= 5 || hour < 1;
    }
}
