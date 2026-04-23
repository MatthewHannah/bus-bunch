using BusBunch.Functions.Services;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

// Register AAD auth provider (same as the Functions Program.cs does).
SqlAuthenticationProvider.SetProvider(
    SqlAuthenticationMethod.ActiveDirectoryDefault,
    new ActiveDirectoryAuthenticationProvider());

var sqlConn = Environment.GetEnvironmentVariable("SqlConnectionString")
    ?? "Server=tcp:busbunch-sql-dkhvkrmhbltmw.database.windows.net,1433;Database=busbunch;Authentication=Active Directory Default;Encrypt=True;";
var staticUrl = Environment.GetEnvironmentVariable("Gtfs__StaticUrl")
    ?? "https://itsmarta.com/google_transit_feed/google_transit.zip";

var services = new ServiceCollection();
services.AddLogging(b => b.AddConsole().SetMinimumLevel(LogLevel.Information));
services.AddHttpClient(GtfsRealtimeClient.HttpClientName, c =>
{
    c.Timeout = TimeSpan.FromMinutes(5);
    c.DefaultRequestHeaders.UserAgent.ParseAdd("bus-bunch/0.1 (+bootstrap)");
});
services.AddSingleton(new GtfsOptions
{
    VehiclePositionsUrl = "", TripUpdatesUrl = "", StaticUrl = staticUrl,
});
services.AddSingleton(new SqlOptions { ConnectionString = sqlConn });
services.AddSingleton<StaticGtfsLoader>();

await using var sp = services.BuildServiceProvider();
var loader = sp.GetRequiredService<StaticGtfsLoader>();
var log = sp.GetRequiredService<ILogger<Program>>();

log.LogInformation("Starting static GTFS load...");
var sw = System.Diagnostics.Stopwatch.StartNew();
var result = await loader.LoadAsync(CancellationToken.None);
sw.Stop();
log.LogInformation("Done in {Sec}s: stops={S} routes={R} trips={T} stoptimes={SST}",
    sw.Elapsed.TotalSeconds, result.Stops, result.Routes, result.Trips, result.StopTimes);
