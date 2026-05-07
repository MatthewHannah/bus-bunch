# bus-bunch

Snapshots MARTA's GTFS-realtime feeds into Azure SQL so we can analyze
**headway** and **bus bunching** at any stop, route, and time of day.

## Layout

```
.
├── BusBunch.sln
├── src/
│   └── BusBunch.Functions/    # Azure Functions (C#, .NET 8 isolated worker)
│       ├── Functions/           # Timer-triggered pollers
│       ├── Services/            # Feed fetch, SQL writer, derivation
│       └── Protos/              # gtfs-realtime.proto (compiled at build)
├── sql/                         # DDL + views (raw, fact, dim)
├── scripts/                     # Python explorers / one-off utilities
└── infra/                       # (later) Azure deployment (Bicep / azd)
```

## Data shape (high level)

- **`snapshot`** — one row per poll.
- **`vehicle_position_snapshot`** — bus GPS at each snapshot.
- **`trip_stop_prediction`** — raw upcoming-stop predictions per snapshot.
- **`stop_arrival_event`** — derived fact table: one row per real-world arrival
  of a bus at a stop. Built inline at the end of each poll by diffing the
  prediction set against the previous snapshot.
- **`stop`, `route`, `trip`, `scheduled_stop_time`** — dimensions loaded from
  MARTA's static GTFS (refresh weekly).
- **`tracked_route`** + **`prediction_history`** — opt-in, per-route capture
  of MARTA's full prediction trajectory, used to measure ETA prediction
  accuracy. INSERT a `route_id` into `tracked_route` to start recording.

Headway at a stop = `LAG(observed_arrival_ts)` over `stop_arrival_event`
partitioned by `(stop_id, route_id, direction_id)`.

Bunching = `actual_headway / scheduled_headway`.

## Local dev

```sh
cd src/BusBunch.Functions
cp local.settings.json.example local.settings.json
# edit the SqlConnectionString connection string
func start
```

## Cost target

- **Azure Functions:** Consumption free tier (1M execs / 400k GB-s per month).
  Adaptive 30s polling = ~86k execs/mo.
- **Azure SQL:** Basic tier, 5 GiB, ~$5/month.
