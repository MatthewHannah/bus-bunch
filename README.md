# bus-bunch

Snapshots MARTA's GTFS-realtime feeds into Azure SQL so we can analyze
**headway** and **bus bunching** at any stop, route, and time of day.

## Layout

```
.
├── BusBunch.sln
├── src/
│   └── BusBunch.Functions/    # Azure Functions (C#, .NET 8 isolated worker)
│       ├── Functions/           # Timer-triggered pollers + HTTP API
│       │   └── Api/             #   Read-only JSON endpoints for the viz site
│       ├── Services/            # Feed fetch, SQL writer, derivation
│       └── Protos/              # gtfs-realtime.proto (compiled at build)
├── web/                         # React + Plotly.js visualization site (Vite)
├── sql/                         # DDL + views (raw, fact, dim)
├── scripts/                     # Python explorers / one-off utilities
└── infra/                       # Azure deployment (Bicep)
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

## Visualization site (web/)

A static React + Plotly.js site that renders four on-demand plots backed
by the Functions HTTP API:

| Page | Endpoint | What it shows |
|---|---|---|
| Marey | `GET /api/marey` | string-line plot for a `(route_short_name, direction)` over the last N hours |
| Vehicle track | `GET /api/vehicle-track` | map trail for one `vehicle_id` with a play/slider |
| Prediction error | `GET /api/prediction-error` | per-trip (predicted − observed) minutes over an ET window, for one stop |
| Prediction evolution | `GET /api/prediction-evolution` | per-trip ETA trajectory + observed/scheduled horizontals |

Local dev:

```sh
# terminal 1 — API
cd src/BusBunch.Functions && func start

# terminal 2 — frontend (Vite proxies /api → http://localhost:7071)
cd web && npm install && npm run dev
```

Production hosting is Azure Static Web Apps **Free** tier. The Free SKU
can't proxy `/api/*` to an external Function App (that's a Standard-SKU
linked-backend feature), so the frontend is built with `VITE_API_BASE`
pointing directly at the Function App's `azurewebsites.net` hostname; the
Function App's CORS allowlist (set in `infra/main.bicep`) admits the SWA
origin.

Deploy (GitHub Actions, `.github/workflows/deploy-web.yml`) needs two repo
secrets:

| Secret | Value |
|---|---|
| `AZURE_STATIC_WEB_APPS_API_TOKEN` | from `az staticwebapp secrets list -n <swa> --query properties.apiKey -o tsv` |
| `VIZ_API_BASE` | the `webApiBase` output from the Bicep deployment (e.g. `https://busbunch-fn-xxxx.azurewebsites.net/api`) |

## Cost target

- **Azure Functions:** Consumption free tier (1M execs / 400k GB-s per month).
  Adaptive 30s polling = ~86k execs/mo.
- **Azure SQL:** Standard S1, 10 GiB, ~$20/month. (Originally targeted Basic
  ~$5/mo, but the dataset — kept-forever `stop_arrival_event` + 7d vehicle
  positions + prediction history — outgrew Basic's 2 GiB cap.)
- **Azure Static Web Apps:** Free tier (100 GB bandwidth / mo).
