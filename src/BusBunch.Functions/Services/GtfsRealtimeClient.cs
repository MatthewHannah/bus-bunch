using Google.Protobuf;
using Microsoft.Extensions.Logging;
using TransitRealtime;

namespace BusBunch.Functions.Services;

/// <summary>
/// Fetches and parses MARTA's two GTFS-realtime feeds.
/// </summary>
public class GtfsRealtimeClient
{
    public const string HttpClientName = "gtfs";

    private readonly IHttpClientFactory _httpFactory;
    private readonly GtfsOptions _options;
    private readonly ILogger<GtfsRealtimeClient> _logger;

    public GtfsRealtimeClient(
        IHttpClientFactory httpFactory,
        GtfsOptions options,
        ILogger<GtfsRealtimeClient> logger)
    {
        _httpFactory = httpFactory;
        _options = options;
        _logger = logger;
    }

    public Task<FeedMessage> FetchVehiclePositionsAsync(CancellationToken ct)
        => FetchAsync(_options.VehiclePositionsUrl, ct);

    public Task<FeedMessage> FetchTripUpdatesAsync(CancellationToken ct)
        => FetchAsync(_options.TripUpdatesUrl, ct);

    private async Task<FeedMessage> FetchAsync(string url, CancellationToken ct)
    {
        var http = _httpFactory.CreateClient(HttpClientName);
        using var resp = await http.GetAsync(url, ct);
        resp.EnsureSuccessStatusCode();
        var bytes = await resp.Content.ReadAsByteArrayAsync(ct);
        var feed = FeedMessage.Parser.ParseFrom(bytes);
        _logger.LogDebug("Fetched {Url}: {Bytes} bytes, {Entities} entities",
            url, bytes.Length, feed.Entity.Count);
        return feed;
    }
}

public sealed class GtfsOptions
{
    public string VehiclePositionsUrl { get; init; } = default!;
    public string TripUpdatesUrl { get; init; } = default!;
    public string StaticUrl { get; init; } = default!;
}
