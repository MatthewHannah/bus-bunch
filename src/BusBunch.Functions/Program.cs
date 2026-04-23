using BusBunch.Functions.Services;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Azure.Functions.Worker.Builder;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

// Microsoft.Data.SqlClient 6+ no longer auto-registers the Active Directory
// authentication providers. Register the default provider explicitly so the
// connection string Authentication=Active Directory Default works (uses
// managed identity in Azure, az login locally).
SqlAuthenticationProvider.SetProvider(
    SqlAuthenticationMethod.ActiveDirectoryDefault,
    new ActiveDirectoryAuthenticationProvider());

var builder = FunctionsApplication.CreateBuilder(args);

builder.ConfigureFunctionsWebApplication();

builder.Services
    .AddApplicationInsightsTelemetryWorkerService()
    .ConfigureFunctionsApplicationInsights();

// HttpClient for MARTA feeds (with a sensible UA).
// The tracker feed serves gzipped blobs (Content-Encoding: gzip), so we enable
// automatic decompression — HttpClient will request gzip and transparently
// decompress on the response.
builder.Services.AddHttpClient(GtfsRealtimeClient.HttpClientName, c =>
{
    c.Timeout = TimeSpan.FromSeconds(20);
    c.DefaultRequestHeaders.UserAgent.ParseAdd("bus-bunch/0.1 (+github)");
})
.ConfigurePrimaryHttpMessageHandler(() => new HttpClientHandler
{
    AutomaticDecompression = System.Net.DecompressionMethods.GZip
                           | System.Net.DecompressionMethods.Deflate,
});

// Bind options
builder.Services.AddSingleton(sp =>
{
    var cfg = sp.GetRequiredService<IConfiguration>();
    return new GtfsOptions
    {
        VehiclePositionsUrl = cfg["Gtfs:VehiclePositionsUrl"]
            ?? "https://tracker.itsmarta.com/gtfs/vehiclepositions.pb",
        TripUpdatesUrl = cfg["Gtfs:TripUpdatesUrl"]
            ?? "https://tracker.itsmarta.com/gtfs/tripupdates.pb",
        StaticUrl = cfg["Gtfs:StaticUrl"]
            ?? "https://itsmarta.com/google_transit_feed/google_transit.zip",
    };
});

builder.Services.AddSingleton(sp =>
{
    var cfg = sp.GetRequiredService<IConfiguration>();
    return new SqlOptions
    {
        ConnectionString = cfg.GetConnectionString("SqlConnectionString")
            ?? cfg["SqlConnectionString"]
            ?? throw new InvalidOperationException(
                "SqlConnectionString connection string is not configured. Set it in local.settings.json " +
                "(Values:SqlConnectionString) or as an app setting.")
    };
});

builder.Services.AddSingleton(sp =>
{
    var cfg = sp.GetRequiredService<IConfiguration>();
    var raw = cfg["Retention:VehiclePositionDays"];
    return new RetentionOptions
    {
        VehiclePositionDays = int.TryParse(raw, out var n) && n > 0 ? n : 7,
    };
});

builder.Services.AddSingleton<GtfsRealtimeClient>();
builder.Services.AddSingleton<SnapshotWriter>();
builder.Services.AddSingleton<StaticGtfsLoader>();
builder.Services.AddSingleton<VehiclePositionPruner>();

builder.Build().Run();
