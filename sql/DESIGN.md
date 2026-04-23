# bus-bunch database design (v3)

Living design doc for the `busbunch` Azure SQL database.

> **Server:** `busbunch-sql-dkhvkrmhbltmw.database.windows.net`
> **Database:** `busbunch` (Azure SQL, Basic 5 DTU tier, 2 GB cap)
> **Auth:** Entra-only (managed identity for the Function App; users via `az login`)
> **DDL files:** `sql/001_*.sql` … `sql/006_*.sql`. Apply in numerical order.

---

## 1. The big idea (v3)

`trip_stop_prediction` is **live state**, not an append log.

Each poll:
1. SqlBulkCopy the parsed prediction set into `staging_predictions`.
2. Call `sp_ProcessPredictionPoll` which:
   - **MERGE**s staging → `trip_stop_prediction` keyed on `(trip_id, stop_sequence)`.
   - Anything that was in `trip_stop_prediction` and *not* in this poll → that bus arrived → emit a row to `stop_arrival_event` and DELETE it from the live table.
   - INSERT a heartbeat row into `snapshot` with diagnostic counters.
   - TRUNCATE `staging_predictions` for the next poll.

Outcome: `trip_stop_prediction` size is bounded by MARTA's prediction set (~17 K rows), not by polling cadence × time. The DB no longer grows linearly with uptime.

The previous (v1/v2) design appended a full prediction snapshot per poll and pruned older ones on a schedule; at MARTA scale this filled a 2 GB Basic database in ~4½ hours.

---

## 2. Tables

### `dbo.snapshot` — heartbeat, kept forever
| col | type | notes |
|---|---|---|
| `snapshot_ts` | `datetime2(0)` PK | when the Function ran (UTC) |
| `feed_ts` | `datetime2(0)` | `FeedHeader.timestamp` from the trip feed |
| `vehicle_entity_n`, `trip_entity_n` | `int` | feed entity counts |
| `poll_duration_ms` | `int` | end-to-end poll latency |
| `upsert_n`, `disappeared_n`, `derived_n` | `int` | per-poll counters from `sp_ProcessPredictionPoll` |
| `skipped_reason` | `varchar(32)` | NULL on healthy polls; `'gap_too_large'` or `'mass_dropout'` when the proc skipped derivation |

### `dbo.trip_stop_prediction` — LIVE prediction state
PK clustered on `(trip_id, stop_sequence)`. One row per currently-predicted (trip, stop). Includes `first_seen_ts` and `last_seen_ts` for diagnostics; `last_seen_ts` is what disambiguates "disappeared this poll" from "still here".

### `dbo.staging_predictions` — SqlBulkCopy target
Same shape as `trip_stop_prediction` minus the `*_seen_ts` columns. Truncated by `sp_ProcessPredictionPoll` at the end of each successful poll.

### `dbo.vehicle_position_snapshot` — append, 7-day rolling
PK clustered on `(snapshot_ts, vehicle_id)`. **No NCI** in v3 — at 7-day retention (~2.3 M rows) the NCI cost was ~400 MB; ad-hoc "trail of one bus" queries can scan the clustered table in a few hundred ms. Re-add the `(vehicle_id, snapshot_ts)` NCI if it becomes a hot path. Pruned hourly by `sp_PruneVehiclePositions`.

### `dbo.stop_arrival_event` — derived fact, kept forever
Same shape as v2. `trip_start_date NOT NULL DEFAULT ''` so `UX_arrival_dedupe (trip_id, trip_start_date, stop_sequence)` works correctly (SQL Server's UNIQUE allows only one NULL — would silently break dedup).

### Static GTFS dimensions (`stop`, `route`, `trip`, `scheduled_stop_time`, `gtfs_static_version`)
Unchanged from v1/v2. Re-loaded weekly by `LoadStaticGtfsFunction`. **Static and realtime feeds use different `route_id` schemes; join via `trip_id` instead.**

---

## 3. Procedures

### `sp_ProcessPredictionPoll @snapshot_ts, @feed_ts, @poll_duration_ms, @vehicle_entity_n, @trip_entity_n`
The per-poll engine. See `004_sp_process_poll.sql` header for the full decision tree.

**Outage skips.** When `gap_s > 300` OR `staging` empty with `live > 0` OR `disappeared/live > 0.5`, the proc:
- does NOT MERGE,
- does NOT delete,
- does NOT emit events,
- *does* INSERT the heartbeat with `skipped_reason` set,
- does NOT truncate staging (keeps the data around in case you want to inspect it).

The next healthy poll re-MERGES; whatever genuinely arrived during the gap will look like normal disappearances then. Cost: slightly stale `last_seen_ts` for one poll. Benefit: no fake arrivals during feed glitches.

**`prev_snapshot_ts` is the most recent NON-SKIPPED snapshot**, not the most recent of any kind — so a chain of skipped polls doesn't poison the next derive.

### `sp_PruneVehiclePositions @cutoff_ts`
Batched delete (5000 rows/batch) from `vehicle_position_snapshot`. Called hourly by `PruneSnapshotsFunction` with `cutoff_ts = now - 7 days`.

---

## 4. Views

| View | Purpose |
|---|---|
| `vw_latest_snapshot` | most recent **non-skipped** poll |
| `vw_live_eta` | current prediction set + stop names + route short names + eta_seconds |
| `vw_stop_headway` | LAG-based per-route headway, high/medium confidence only |
| `vw_stop_headway_with_sched` | actual + scheduled headway + bunching ratio (joins schedule via `trip_id`) |
| `vw_stop_trunk_headway` | route-agnostic headway per stop |

---

## 5. Sizing & retention

Steady-state estimates on Basic 2 GB cap:

| Table | Size | Retention |
|---|---:|---|
| `snapshot` | ~50 KB/month | forever |
| `trip_stop_prediction` (live) | ~3 MB constant | live state — never grows |
| `staging_predictions` | ~3 MB at peak (truncated every poll) | per-poll |
| `vehicle_position_snapshot` | ~250 MB at 7 days | 7-day rolling |
| `stop_arrival_event` | grows; ~150 MB/year est. | forever |
| `scheduled_stop_time` | ~90 MB | refreshed weekly (full reload) |
| `trip`, `route`, `stop`, `gtfs_static_version` | ~10 MB total | refreshed weekly |

Total steady-state: ~500 MB headroom on the 2 GB cap, leaving ~1 GB for `stop_arrival_event` growth (~6 years before refactoring/scaling needed).

---

## 6. Conventions

- All timestamps stored in UTC as `datetime2(0)`.
- IDs are `varchar` — MARTA's are opaque strings.
- Column names mirror GTFS terminology so SQL reads next to the proto with no translation.
- Migrations are append-only files numbered `00N_…sql`. Re-running an older file is safe (drops are guarded by `IF OBJECT_ID … IS NOT NULL`); altering an existing object means a new numbered file.
