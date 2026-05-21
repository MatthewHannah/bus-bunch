# bus-bunch local viz

Jupyter-compatible scripts for exploring bunching data in the `busbunch` SQL
database. Percent-format (`# %%`) `.py` files — open them in VS Code or
convert with `jupytext`.

## One-time setup

```bash
# 1. Microsoft ODBC driver (needed by pyodbc)
brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
brew update
HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18

# 2. Conda environment
cd scripts/viz
conda env create -f environment.yml
conda activate bus-bunch-viz

# 3. Point conda's unixODBC at brew's odbcinst.ini (one-time, persisted in env)
#    Conda ships its own unixODBC and won't see the brew-installed driver
#    unless we tell it where odbcinst.ini lives.
conda env config vars set ODBCSYSINI=/usr/local/etc
conda deactivate && conda activate bus-bunch-viz

# 4. Make sure az CLI is logged in (used for DB auth via access token)
az account show >/dev/null || az login
```

## Run

```bash
conda activate bus-bunch-viz
python bunching.py       # 3 charts + bunching_map.html
python marey.py          # string-line diagram
python vehicle_track.py  # one bus's trail on a map, with time slider
python prediction_evolution.py  # how each trip's ETA at one stop evolved
python prediction_drift.py      # same, Y = drift (min) from first prediction
python prediction_error.py      # same, Y = predicted − observed (min)
python vehicle_gps_audit.py     # debug: GPS pings + per-trip arrival counts
```

Or open either file in VS Code — it'll recognize the `# %%` cell markers and
give you a notebook UI with inline Plotly charts.

## Files

- `db.py` — shared connection helper. `query(sql, params)` returns a DataFrame
  using your `az login` credential.
- `bunching.py` — system-wide dashboards: headway histogram for one stop,
  bunching index over time, geographic bubble map (Plotly + Folium).
- `marey.py` — string-line diagram for a single route+direction. Time on X,
  stop sequence on Y, one line per bus run. Bunching = lines converging.
- `vehicle_track.py` — single-bus trail on a map for one `vehicle_id` over
  the last N hours, with a time slider that highlights the bus's position
  at the chosen minute.
- `prediction_evolution.py` — for one stop, plots each upcoming bus's
  predicted-arrival ETA as it evolved across successive polls, with the
  observed and scheduled arrivals overlaid per trip. Requires the route to
  be in `dbo.tracked_route`.
- `prediction_drift.py` — same data as `prediction_evolution.py`, but the Y
  axis is the *difference* (in minutes) between each prediction and that
  trip's first observed prediction. Flat ≈ 0 = stable ETA; rising = MARTA
  kept pushing the arrival later.
- `prediction_error.py` — same data again, Y = `predicted − observed`
  (minutes). Anchored on ground truth: every line should taper to 0 as the
  bus arrives. Skips trips with no derived `stop_arrival_event`.
- `vehicle_gps_audit.py` — debug tool. For one `vehicle_id` and time window,
  shows a per-trip summary (with derived-arrival counts joined in, so
  "trip had 40 pings, 0 arrivals" jumps out in red) plus the full raw GPS
  ping table. Also writes the raw pings to a CSV.
- `environment.yml` — conda spec.
