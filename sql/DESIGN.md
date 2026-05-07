# bus-bunch database design (v3)

Living design doc for the `busbunch` Azure SQL database.

> **Server:** `busbunch-sql-dkhvkrmhbltmw.database.windows.net`
> **Database:** `busbunch` (Azure SQL, Basic 5 DTU tier, 2 GB cap)
> **Auth:** Entra-only (managed identity for the Function App; users via `az login`)
> **DDL files:** `sql/001_*.sql` … `sql/012_*.sql`. Apply in numerical order.

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
| `upsert_n`, `disappeared_n`, `derived_n`, `resurrected_n` | `int` | per-poll counters from `sp_ProcessPredictionPoll` (`resurrected_n` is NULL on rows written before migration `007`) |
| `skipped_reason` | `varchar(32)` | NULL on healthy polls; `'gap_too_large'` (resync — live state was MERGEd, derive skipped), `'gap_no_data'` (long gap with empty staging, nothing done), or `'mass_dropout'` (suspicious staging, nothing done) |

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

### `dbo.tracked_route` + `dbo.prediction_history` — ETA accuracy capture (migrations `009`–`012`)

Opt-in, per-route recording of MARTA's full prediction trajectory.

`tracked_route (route_id PK)` is the config table. Empty by default; `INSERT INTO tracked_route (route_id) VALUES ('21')` to start recording. Effects start on the next poll.

`prediction_history` is append-per-poll, holding one row per `(snapshot_ts, route_id, trip_id, trip_start_date, stop_sequence)` for any route in `tracked_route`. Clustered on `snapshot_ts` first so the storage-target pruner can do cheap range deletes (mirrors `vehicle_position_snapshot`); NCI on `(trip_id, trip_start_date, stop_sequence, snapshot_ts)` for the analytical join into `stop_arrival_event`. PAGE compression.

The recorder (`sp_RecordPredictionHistory`) is intentionally decoupled from `sp_ProcessPredictionPoll`. It runs **before** ProcessPoll in the same transaction and is unconditional — predictions are recorded on `gap_too_large` / `gap_no_data` / `mass_dropout` polls too, because those events ARE part of MARTA's behavior we want to measure.

Pruning is storage-target, not time-based: `sp_PrunePredictionHistory @target_mb` walks the table from oldest to newest deleting one hour at a time until used MB ≤ target. `vw_prediction_history_window` exposes current `oldest_snapshot_ts` / `used_mb` so analysts can see the effective retention. The DMV read requires `VIEW DATABASE STATE`, granted to PUBLIC in `011`.

Analytical view: `vw_prediction_error` — LEFT JOIN history → arrival, exposes `horizon_seconds`, signed `error_seconds`, and `arrival_resolution ∈ {matched, suspect_cancelled, unresolved}`.

**Currently tracked:** `26916` (Route 21 — Memorial Drive ITP), `26917` (Route 22 — Glenwood). Note the realtime feed currently uses the same numeric `route_id`s as static GTFS (the cross-feed mismatch caveat in §3 may be out of date for buses; verify per-route).

**Sign convention:** `error_seconds = predicted - observed`. Positive = MARTA was pessimistic (bus arrived earlier than predicted). Negative = MARTA was optimistic (bus arrived later than predicted).

#### Canonical example queries

Accuracy curve — median absolute error and P90 by horizon bucket, last 24h:
```sql
SELECT
    CASE
        WHEN horizon_seconds <   60 THEN '0-1m'
        WHEN horizon_seconds <  300 THEN '1-5m'
        WHEN horizon_seconds <  600 THEN '5-10m'
        WHEN horizon_seconds < 1200 THEN '10-20m'
        ELSE '20m+'
    END AS horizon_bucket,
    COUNT(*) AS n,
    APPROX_PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ABS(error_seconds)) AS abs_err_p50,
    APPROX_PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ABS(error_seconds)) AS abs_err_p90,
    AVG(CAST(error_seconds AS float))                                       AS bias_mean
FROM dbo.vw_prediction_error
WHERE arrival_resolution = 'matched'
  AND snapshot_ts > DATEADD(HOUR, -24, SYSUTCDATETIME())
GROUP BY
    CASE
        WHEN horizon_seconds <   60 THEN '0-1m'
        WHEN horizon_seconds <  300 THEN '1-5m'
        WHEN horizon_seconds <  600 THEN '5-10m'
        WHEN horizon_seconds < 1200 THEN '10-20m'
        ELSE '20m+'
    END
ORDER BY MIN(horizon_seconds);
```

Per-route bias (is MARTA systematically over- or under-predicting?):
```sql
SELECT route_id, route_short_name,
       COUNT(*) AS n,
       AVG(CAST(error_seconds AS float)) AS bias_mean_seconds,
       APPROX_PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY error_seconds) AS bias_median
FROM dbo.vw_prediction_error
WHERE arrival_resolution = 'matched'
  AND horizon_seconds BETWEEN 60 AND 600
GROUP BY route_id, route_short_name;
```

Trajectory of a single trip's predictions for one stop (great for marey-style plots):
```sql
SELECT snapshot_ts, predicted_arrival_ts, observed_arrival_ts,
       horizon_seconds, error_seconds
FROM dbo.vw_prediction_error
WHERE trip_id = '<trip_id>' AND stop_sequence = <n>
ORDER BY snapshot_ts;
```

How much data is queryable right now:
```sql
SELECT * FROM dbo.vw_prediction_history_window;
```

---

## 3. Procedures

### `sp_ProcessPredictionPoll @snapshot_ts, @feed_ts, @poll_duration_ms, @vehicle_entity_n, @trip_entity_n`
The per-poll engine. See `004_sp_process_poll.sql` header for the full decision tree.

**Outage skips.** Three flavors:

| Reason | Trigger | What runs |
|---|---|---|
| `gap_too_large` | `gap_s > 300` AND `staging_n > 0` | MERGE + resurrection + delete-stale (live state is **resynced** to current staging). Derive is skipped — we can't fabricate arrival timestamps across a multi-hour gap. Heartbeat is recorded; staging is truncated. **Counts as a baseline** for the next poll. |
| `gap_no_data`   | `gap_s > 300` AND `staging_n = 0` | Nothing — no MERGE, no delete, no derive. Does **not** count as a baseline. The next poll with data will trip `gap_too_large` and resync. |
| `mass_dropout`  | (`live > 0` AND `staging` empty) OR `disappeared/live > 0.5` | Nothing. Does **not** count as a baseline. Self-healing: the next healthy poll's gap from the last good snapshot is normally still small. |

In all three cases the heartbeat row is INSERTed with `skipped_reason` set so the warning surfaces in logs (`SnapshotWriter.cs`), and staging is truncated.

**`prev_snapshot_ts` = most recent snapshot with `skipped_reason IS NULL OR skipped_reason = 'gap_too_large'`.** Healthy polls and trusted gap-resyncs are valid baselines; `gap_no_data` and `mass_dropout` are not (they didn't update live state). Before migration `008` only NULL counted, which meant a single `gap_too_large` could permanently stall the system — the baseline never advanced, so every subsequent poll re-tripped `gap_too_large`.

**Resurrection (migration `007`).** When MARTA temporarily drops a trip's stops from `tripupdates` mid-route, the previous poll's derive emits `suspect_cancelled` arrivals (with `observed_arrival_ts = @snapshot_ts`, confidence `low`). If MARTA later resumes publishing predictions for those same `(trip_id, trip_start_date, stop_sequence)` keys, the proc — immediately after the MERGE — DELETEs those bogus events. This frees `UX_arrival_dedupe` so the bus's actual later arrival can be derived properly. Counter exposed as `snapshot.resurrected_n`. Forward-only: historical bogus rows from before `007` deployed are not retroactively cleaned (their real arrival times are unrecoverable since the prediction history isn't kept).

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
| `vw_prediction_error` | per-prediction signed error vs. eventual arrival, with horizon and `arrival_resolution` |
| `vw_prediction_history_window` | ops view: current `oldest_snapshot_ts`, `used_mb`, `tracked_route_n` |

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
| `prediction_history` (per tracked route) | ~50–70 MB/route/day (incl. NCI) | storage-target (default 600 MB cap) |
| `scheduled_stop_time` | ~90 MB | refreshed weekly (full reload) |
| `trip`, `route`, `stop`, `gtfs_static_version` | ~10 MB total | refreshed weekly |

Total steady-state: ~500 MB headroom on the 2 GB cap, leaving ~1 GB for `stop_arrival_event` growth (~6 years before refactoring/scaling needed).

---

## 6. Conventions

- All timestamps stored in UTC as `datetime2(0)`.
- IDs are `varchar` — MARTA's are opaque strings.
- Column names mirror GTFS terminology so SQL reads next to the proto with no translation.
- Migrations are append-only files numbered `00N_…sql`. Re-running an older file is safe (drops are guarded by `IF OBJECT_ID … IS NOT NULL`); altering an existing object means a new numbered file.
