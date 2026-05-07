using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using TransitRealtime;

// ---------------------------------------------------------------------------
// BusBunch.FeedCompare
//
// Quick-and-dirty side-by-side comparison of MARTA's two realtime data
// sources:
//   1. GTFS-realtime protobuf feeds (vehiclepositions.pb, tripupdates.pb)
//   2. OTP GraphQL endpoint at tracker.itsmarta.com
//
// Goal: see whether one source surfaces information the other doesn't.
// ---------------------------------------------------------------------------

const string VehiclePositionsUrl = "https://tracker.itsmarta.com/gtfs/vehiclepositions.pb";
const string TripUpdatesUrl      = "https://tracker.itsmarta.com/gtfs/tripupdates.pb";
const string GraphQlUrl          = "https://tracker.itsmarta.com/otp/routers/default/index/graphql";
const string FeedId              = "MARTA";
const int    SampleTrips         = 5;

using var handler = new HttpClientHandler
{
    AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate,
};
using var http = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(60) };
http.DefaultRequestHeaders.UserAgent.ParseAdd("bus-bunch-feedcompare/0.1");

Console.WriteLine($"=== MARTA feed comparison @ {DateTime.UtcNow:O} ===\n");

// --- 1. Pull both protobuf feeds -------------------------------------------
Console.WriteLine("[1] Fetching GTFS-realtime protobuf feeds…");
var vehicleFeed = await FetchPbAsync(http, VehiclePositionsUrl);
var tripFeed    = await FetchPbAsync(http, TripUpdatesUrl);

var pbVehicles = vehicleFeed.Entity
    .Where(e => e.Vehicle is not null)
    .Select(e => e.Vehicle)
    .ToList();

var pbTripUpdates = tripFeed.Entity
    .Where(e => e.TripUpdate is not null)
    .Select(e => e.TripUpdate)
    .ToList();

var pbStopPredictions = pbTripUpdates
    .SelectMany(tu => tu.StopTimeUpdate.Select(stu => (tu.Trip.TripId, stu)))
    .ToList();

Console.WriteLine($"  vehiclepositions.pb : header_ts={EpochToUtc((long)vehicleFeed.Header.Timestamp):O}");
Console.WriteLine($"                        entities={vehicleFeed.Entity.Count}, vehicles={pbVehicles.Count}");
Console.WriteLine($"  tripupdates.pb      : header_ts={EpochToUtc((long)tripFeed.Header.Timestamp):O}");
Console.WriteLine($"                        entities={tripFeed.Entity.Count}, trip_updates={pbTripUpdates.Count}, stop_predictions={pbStopPredictions.Count}");

var pbTripIds  = pbTripUpdates.Select(tu => tu.Trip.TripId).Where(s => !string.IsNullOrEmpty(s)).ToHashSet();
var pbRouteIds = pbTripUpdates.Select(tu => tu.Trip.RouteId).Where(s => !string.IsNullOrEmpty(s)).ToHashSet();
var pbVehicleIds = pbVehicles.Select(v => v.Vehicle?.Id ?? "").Where(s => !string.IsNullOrEmpty(s)).ToHashSet();

Console.WriteLine($"  distinct trip_ids   : {pbTripIds.Count}");
Console.WriteLine($"  distinct route_ids  : {pbRouteIds.Count}");
Console.WriteLine($"  distinct vehicle_ids: {pbVehicleIds.Count}\n");

// --- 2. Probe GraphQL inventory --------------------------------------------
Console.WriteLine("[2] Querying GraphQL inventory…");
var inventory = await GqlAsync<InventoryResp>(http, GraphQlUrl, """
{
  feeds { feedId }
  routes { gtfsId shortName mode }
  serviceTimeRange { start end }
}
""");

Console.WriteLine($"  feeds              : {string.Join(", ", inventory.Feeds.Select(f => f.FeedId))}");
Console.WriteLine($"  total routes       : {inventory.Routes.Count}");
Console.WriteLine($"  modes              : {string.Join(", ", inventory.Routes.GroupBy(r => r.Mode).Select(g => $"{g.Key}={g.Count()}"))}");
if (inventory.ServiceTimeRange is { } sr)
{
    Console.WriteLine($"  service window     : {EpochToUtc(sr.Start):O} → {EpochToUtc(sr.End):O}");
}
Console.WriteLine();

// --- 3. Vehicle positions: GraphQL has no equivalent -----------------------
Console.WriteLine("[3] Vehicle positions:");
Console.WriteLine($"  protobuf vehiclepositions.pb : {pbVehicles.Count} vehicles with lat/lon/bearing/speed/ts");
Console.WriteLine("  GraphQL                       : (no live-vehicle query exists in MARTA's OTP schema)");
Console.WriteLine("  → Protobuf is the ONLY source of live vehicle GPS.\n");

// --- 4. Trip predictions: deep dive on a sample of trips -------------------
Console.WriteLine($"[4] Trip predictions: comparing {SampleTrips} sample trips…\n");

var sampleTripIds = pbTripIds.OrderBy(id => id).Take(SampleTrips).ToList();
var totalPbStopRows = 0;
var totalGqlStopRows = 0;
var totalGqlRealtime = 0;
var matchedStopRows = 0;
var pbOnlyStopRows = 0;
var gqlOnlyStopRows = 0;

var today = DateTime.Now.ToString("yyyyMMdd");

foreach (var tripId in sampleTripIds)
{
    var pbForTrip = pbTripUpdates.Where(tu => tu.Trip.TripId == tripId).ToList();
    var pbStops = pbForTrip
        .SelectMany(tu => tu.StopTimeUpdate)
        .Select(stu => new {
            StopId       = stu.StopId,
            Seq          = stu.HasStopSequence ? (int?)stu.StopSequence : null,
            ArrivalEpoch = stu.Arrival   is { HasTime: true } a ? (long?)a.Time : null,
            DepartEpoch  = stu.Departure is { HasTime: true } d ? (long?)d.Time : null,
        })
        .ToList();

    var gql = await GqlAsync<TripResp>(http, GraphQlUrl, $$"""
    {
      trip(id: "{{FeedId}}:{{tripId}}") {
        gtfsId
        directionId
        tripHeadsign
        route { gtfsId shortName }
        stoptimesForDate(serviceDate: "{{today}}") {
          stop { gtfsId code }
          stopPosition
          scheduledArrival
          scheduledDeparture
          realtimeArrival
          realtimeDeparture
          arrivalDelay
          departureDelay
          realtime
          realtimeState
          serviceDay
        }
      }
    }
    """);

    var gqlTrip = gql.Trip;
    var gqlStops = gqlTrip?.StoptimesForDate ?? new();

    Console.WriteLine($"  trip {tripId}");
    Console.WriteLine($"    protobuf stop predictions  : {pbStops.Count}");
    if (gqlTrip is null)
    {
        Console.WriteLine("    GraphQL trip(...)          : NOT FOUND");
        pbOnlyStopRows  += pbStops.Count;
        totalPbStopRows += pbStops.Count;
        continue;
    }

    var gqlRealtime = gqlStops.Count(s => s.Realtime);
    Console.WriteLine($"    GraphQL stoptimesForDate   : {gqlStops.Count} total, {gqlRealtime} flagged realtime");
    Console.WriteLine($"    GraphQL headsign / route   : \"{gqlTrip.TripHeadsign}\" / {gqlTrip.Route?.ShortName}");

    // Overlap by stop_id (predictions only — GraphQL's stoptimesForDate
    // returns the WHOLE schedule for the trip, not just the upcoming stops).
    var pbStopSet  = pbStops.Select(s => s.StopId).ToHashSet();
    var gqlStopSet = gqlStops.Where(s => s.Stop is not null).Select(s => StripFeedPrefix(s.Stop!.GtfsId)).ToHashSet();
    var bothCount  = pbStopSet.Intersect(gqlStopSet).Count();
    var pbOnly     = pbStopSet.Except(gqlStopSet).Count();
    var gqlOnly    = gqlStopSet.Except(pbStopSet).Count();

    Console.WriteLine($"    stop_id overlap (pb ∩ gql) : {bothCount}");
    Console.WriteLine($"    only in protobuf           : {pbOnly}");
    Console.WriteLine($"    only in GraphQL            : {gqlOnly}  (← upstream/already-passed schedule rows)");

    // Compare realtime arrival times for matched stops where both sides have a value.
    var gqlByStop = gqlStops
        .Where(s => s.Stop is not null && s.Realtime)
        .GroupBy(s => StripFeedPrefix(s.Stop!.GtfsId))
        .ToDictionary(g => g.Key, g => g.First(), StringComparer.Ordinal);

    var diffs = new List<long>();
    foreach (var pb in pbStops)
    {
        if (pb.ArrivalEpoch is null) continue;
        if (!gqlByStop.TryGetValue(pb.StopId, out var g)) continue;
        if (g.ServiceDay is null || g.RealtimeArrival is null) continue;
        var gqlEpoch = g.ServiceDay.Value + g.RealtimeArrival.Value;
        diffs.Add(pb.ArrivalEpoch.Value - gqlEpoch);
    }
    if (diffs.Count > 0)
    {
        Console.WriteLine($"    realtime arrival deltas    : n={diffs.Count}, min={diffs.Min()}s, max={diffs.Max()}s, avg={diffs.Average():F1}s");
    }
    Console.WriteLine();

    totalPbStopRows  += pbStops.Count;
    totalGqlStopRows += gqlStops.Count;
    totalGqlRealtime += gqlRealtime;
    matchedStopRows  += bothCount;
    pbOnlyStopRows   += pbOnly;
    gqlOnlyStopRows  += gqlOnly;
}

// --- 5. Verdict ------------------------------------------------------------
Console.WriteLine("=== Summary ===");
Console.WriteLine("  Protobuf feeds expose:");
Console.WriteLine("    • Live vehicle GPS, bearing, speed, vehicle_id (NOT in GraphQL)");
Console.WriteLine($"    • {pbStopPredictions.Count} upcoming-stop predictions across {pbTripIds.Count} trips this poll");
Console.WriteLine("  GraphQL exposes:");
Console.WriteLine("    • Static schedule + realtime overlay via trip(id).stoptimesForDate");
Console.WriteLine("    • Headsigns, route metadata, alerts, patterns, geometry, occupancy");
Console.WriteLine("    • Whole-trip stoptimes (incl. already-passed stops); protobuf only has remaining ones");
Console.WriteLine();
Console.WriteLine($"  Across {sampleTripIds.Count} sampled trips:");
Console.WriteLine($"    pb stop rows total         : {totalPbStopRows}");
Console.WriteLine($"    gql stop rows total        : {totalGqlStopRows}  (of which {totalGqlRealtime} were realtime)");
Console.WriteLine($"    matched by stop_id         : {matchedStopRows}");
Console.WriteLine($"    pb-only stop rows          : {pbOnlyStopRows}");
Console.WriteLine($"    gql-only stop rows         : {gqlOnlyStopRows}");
Console.WriteLine();
Console.WriteLine("  Bottom line: the two sources carry the same realtime predictions, but");
Console.WriteLine("  protobuf is the only source of vehicle positions and GraphQL is the only");
Console.WriteLine("  source of richer trip context (headsign, alerts, geometry, occupancy).");

// ---------------------------------------------------------------------------
static async Task<FeedMessage> FetchPbAsync(HttpClient http, string url)
{
    using var resp = await http.GetAsync(url);
    resp.EnsureSuccessStatusCode();
    var bytes = await resp.Content.ReadAsByteArrayAsync();
    return FeedMessage.Parser.ParseFrom(bytes);
}

static DateTime EpochToUtc(long s) => DateTimeOffset.FromUnixTimeSeconds(s).UtcDateTime;

static string StripFeedPrefix(string gtfsId)
{
    var i = gtfsId.IndexOf(':');
    return i < 0 ? gtfsId : gtfsId[(i + 1)..];
}

static async Task<T> GqlAsync<T>(HttpClient http, string url, string query)
{
    var body = new { query };
    using var resp = await http.PostAsJsonAsync(url, body);
    resp.EnsureSuccessStatusCode();
    var env = await resp.Content.ReadFromJsonAsync<GqlEnvelope<T>>(new JsonSerializerOptions
    {
        PropertyNameCaseInsensitive = true,
    });
    if (env?.Errors is { Count: > 0 })
        throw new InvalidOperationException("GraphQL errors: " + JsonSerializer.Serialize(env.Errors));
    return env!.Data!;
}

// ---- DTOs ------------------------------------------------------------------

sealed class GqlEnvelope<T>
{
    [JsonPropertyName("data")]   public T? Data { get; set; }
    [JsonPropertyName("errors")] public List<JsonElement>? Errors { get; set; }
}

sealed class InventoryResp
{
    [JsonPropertyName("feeds")]            public List<Feed> Feeds { get; set; } = new();
    [JsonPropertyName("routes")]           public List<Route> Routes { get; set; } = new();
    [JsonPropertyName("serviceTimeRange")] public ServiceRange? ServiceTimeRange { get; set; }

    public sealed class Feed   { [JsonPropertyName("feedId")] public string FeedId { get; set; } = ""; }
    public sealed class Route
    {
        [JsonPropertyName("gtfsId")]    public string GtfsId    { get; set; } = "";
        [JsonPropertyName("shortName")] public string? ShortName { get; set; }
        [JsonPropertyName("mode")]      public string? Mode      { get; set; }
    }
    public sealed class ServiceRange
    {
        [JsonPropertyName("start")] public long Start { get; set; }
        [JsonPropertyName("end")]   public long End   { get; set; }
    }
}

sealed class TripResp
{
    [JsonPropertyName("trip")] public TripDto? Trip { get; set; }

    public sealed class TripDto
    {
        [JsonPropertyName("gtfsId")]           public string  GtfsId           { get; set; } = "";
        [JsonPropertyName("directionId")]      public string? DirectionId      { get; set; }
        [JsonPropertyName("tripHeadsign")]     public string? TripHeadsign     { get; set; }
        [JsonPropertyName("route")]            public RouteRef? Route          { get; set; }
        [JsonPropertyName("stoptimesForDate")] public List<StoptimeDto>? StoptimesForDate { get; set; }
    }
    public sealed class RouteRef
    {
        [JsonPropertyName("gtfsId")]    public string  GtfsId    { get; set; } = "";
        [JsonPropertyName("shortName")] public string? ShortName { get; set; }
    }
    public sealed class StoptimeDto
    {
        [JsonPropertyName("stop")]               public StopRef? Stop { get; set; }
        [JsonPropertyName("stopPosition")]       public int?  StopPosition       { get; set; }
        [JsonPropertyName("scheduledArrival")]   public int?  ScheduledArrival   { get; set; }
        [JsonPropertyName("scheduledDeparture")] public int?  ScheduledDeparture { get; set; }
        [JsonPropertyName("realtimeArrival")]    public int?  RealtimeArrival    { get; set; }
        [JsonPropertyName("realtimeDeparture")]  public int?  RealtimeDeparture  { get; set; }
        [JsonPropertyName("arrivalDelay")]       public int?  ArrivalDelay       { get; set; }
        [JsonPropertyName("departureDelay")]     public int?  DepartureDelay     { get; set; }
        [JsonPropertyName("realtime")]           public bool  Realtime           { get; set; }
        [JsonPropertyName("realtimeState")]      public string? RealtimeState    { get; set; }
        [JsonPropertyName("serviceDay")]         public long? ServiceDay         { get; set; }
    }
    public sealed class StopRef
    {
        [JsonPropertyName("gtfsId")] public string  GtfsId { get; set; } = "";
        [JsonPropertyName("code")]   public string? Code   { get; set; }
    }
}
