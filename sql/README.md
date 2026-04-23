# sql/

DDL for the bus-bunch database. Apply in numerical order against an empty Azure SQL database.

> See [`DESIGN.md`](./DESIGN.md) for the full design.

| File | Purpose |
|---|---|
| `001_grant_mi.sql`                  | Grant the Function App's managed identity access (run once after deploy) |
| `002_realtime.sql`                  | `snapshot`, `vehicle_position_snapshot`, `trip_stop_prediction` (live), `staging_predictions`, `stop_arrival_event` |
| `003_static_gtfs.sql`               | `stop`, `route`, `trip`, `scheduled_stop_time`, `gtfs_static_version` |
| `004_sp_process_poll.sql`           | `dbo.sp_ProcessPredictionPoll` — the per-poll engine (MERGE + derive + heartbeat) |
| `005_sp_prune_vehicle_positions.sql`| `dbo.sp_PruneVehiclePositions` — hourly cleanup of vehicle_position_snapshot |
| `006_views.sql`                     | `vw_latest_snapshot`, `vw_live_eta`, `vw_stop_headway`, `vw_stop_headway_with_sched`, `vw_stop_trunk_headway` |

## Apply against Azure SQL with Entra

```sh
for f in sql/00*.sql; do
  sqlcmd -S YOUR_SERVER.database.windows.net -d busbunch -G -i "$f"
done
```

## Quick headway check

```sql
SELECT TOP 50 *
FROM dbo.vw_stop_headway
WHERE stop_id = '104078'
  AND observed_arrival_ts >= DATEADD(HOUR, -3, SYSUTCDATETIME())
ORDER BY observed_arrival_ts DESC;
```
