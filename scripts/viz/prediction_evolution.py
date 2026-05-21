# %% [markdown]
# # Prediction evolution at a stop
#
# For one stop, show how MARTA's predicted arrival time for each upcoming bus
# *evolved* across successive polls.
#
# - **X axis**: `snapshot_ts` (when MARTA published the prediction, ET)
# - **Y axis**: `predicted_arrival_ts` (the ETA itself, ET)
# - **One colored line** per `(trip_id, trip_start_date)` — one bus's ETA history
# - **Gray diagonal** y = x: the "now" reference. A prediction line *terminates*
#   on the diagonal at the moment the bus is predicted to arrive right now —
#   typically the last poll before the bus shows up at the stop.
# - **Per trip, in the same color**:
#     * **dashed** horizontal at the *observed* arrival (from
#       `stop_arrival_event`) — ground truth the prediction should converge to
#     * **dotted** horizontal at the *scheduled* arrival (from
#       `scheduled_stop_time`) — what was supposed to happen
#
# Reading it:
#   * Flat horizontal line ≈ MARTA's ETA was steady.
#   * Line rising above its dashed observed-line ≈ MARTA kept pushing the ETA
#     later (bus stuck in traffic, late).
#   * Line dropping below ≈ bus arrived sooner than predicted.
#   * Two trips' lines converging on the same dashed observed line ≈ bunching
#     forming in real time.
#
# Requires the route to be in `dbo.tracked_route` so its predictions are being
# recorded. The script prints a friendly error listing tracked routes if not.

# %%
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio

from db import query

pio.renderers.default = "browser"

ET = "America/New_York"

# %% [markdown]
# ## Config

# %%
STOP_ID          = "104076"               # MARTA stop_id
ROUTE_SHORT_NAME = ""                   # realtime/public route number; "" or None = ALL routes serving this stop
DIRECTION_ID     = 0                      # 0 or 1; ignored when ROUTE_SHORT_NAME is empty
WINDOW_START_ET  = "2026-05-12 08:00"     # local (Eastern)
WINDOW_END_ET    = "2026-05-12 12:00"

# When ROUTE_SHORT_NAME is empty/None, we plot every tracked route serving
# this stop; the direction filter is dropped too because direction_id has
# different meanings across routes.
ALL_ROUTES = not ROUTE_SHORT_NAME

# %% [markdown]
# ## 1. Sanity-check that prediction history exists
#
# `prediction_history` only captures routes listed in `dbo.tracked_route`. If
# a specific route was requested but isn't tracked, fail loudly. If ALL routes
# were requested, just confirm at least one route is tracked.

# %%
tracked = query(
    """
    SELECT tr.route_id, r.route_short_name, tr.added_at, tr.note
    FROM dbo.tracked_route tr
    LEFT JOIN dbo.route r ON r.route_id = tr.route_id
    ORDER BY r.route_short_name
    """
)
tracked_short_names = sorted(tracked["route_short_name"].dropna().astype(str).tolist())
if ALL_ROUTES:
    if not tracked_short_names:
        raise SystemExit(
            "dbo.tracked_route is empty, so no predictions are being recorded.\n"
            "Add a route with:  INSERT INTO dbo.tracked_route (route_id) "
            "VALUES ('<route_id>');"
        )
    print(f"All-routes mode. Tracked routes: {tracked_short_names}")
elif ROUTE_SHORT_NAME not in set(tracked_short_names):
    raise SystemExit(
        f"Route {ROUTE_SHORT_NAME!r} is not in dbo.tracked_route, so its "
        f"predictions aren't being recorded.\n"
        f"Currently tracked: {tracked_short_names}\n"
        f"Add it with:  INSERT INTO dbo.tracked_route (route_id) "
        f"VALUES ('<route_id>');"
    )

# %% [markdown]
# ## 2. Pull the prediction trajectories
#
# Convert the local-time window to UTC (snapshot_ts is stored UTC). Always
# filter to the chosen stop and window. When a specific route is set, also
# filter by route_short_name + direction; otherwise capture every tracked
# route serving this stop.
# Prediction rows with NULL `predicted_arrival_ts` get dropped — those are
# SKIPPED stops, not useful for an ETA-evolution chart.
#
# `vw_prediction_error` joins prediction_history to stop_arrival_event and
# scheduled_stop_time for us, so the observed and scheduled values used in
# the next section ride along on the same query.

# %%
window_start_utc = (
    pd.Timestamp(WINDOW_START_ET, tz=ET).tz_convert("UTC").tz_localize(None)
)
window_end_utc = (
    pd.Timestamp(WINDOW_END_ET, tz=ET).tz_convert("UTC").tz_localize(None)
)

base_sql = """
    SELECT
        v.snapshot_ts,
        v.route_id,
        v.route_short_name,
        v.direction_id,
        v.trip_id,
        v.trip_start_date,
        v.stop_sequence,
        v.stop_name,
        v.vehicle_id,
        v.predicted_arrival_ts,
        v.observed_arrival_ts,
        v.derivation,
        v.confidence,
        v.scheduled_arrival_seconds,
        v.arrival_resolution
    FROM dbo.vw_prediction_error v
    WHERE v.stop_id = ?
      AND v.snapshot_ts >= ?
      AND v.snapshot_ts <  ?
      AND v.predicted_arrival_ts IS NOT NULL
"""
params: list = [
    STOP_ID,
    window_start_utc.to_pydatetime(),
    window_end_utc.to_pydatetime(),
]
if not ALL_ROUTES:
    base_sql += """
      AND v.route_short_name = ?
      AND (v.direction_id = ? OR v.direction_id IS NULL)
    """
    params.extend([ROUTE_SHORT_NAME, DIRECTION_ID])
base_sql += " ORDER BY v.route_id, v.trip_id, v.trip_start_date, v.snapshot_ts"

predictions = query(base_sql, tuple(params))
if predictions.empty:
    scope = "any tracked route" if ALL_ROUTES else f"route {ROUTE_SHORT_NAME} dir {DIRECTION_ID}"
    raise SystemExit(
        f"No predictions found for stop {STOP_ID} on {scope} between "
        f"{WINDOW_START_ET} and {WINDOW_END_ET} ET. Widen the window or "
        f"check that the route(s) were being tracked then."
    )

predictions["snapshot_et"] = (
    pd.to_datetime(predictions["snapshot_ts"]).dt.tz_localize("UTC").dt.tz_convert(ET)
)
predictions["predicted_et"] = (
    pd.to_datetime(predictions["predicted_arrival_ts"])
      .dt.tz_localize("UTC")
      .dt.tz_convert(ET)
)
predictions["minutes_out"] = (
    (predictions["predicted_et"] - predictions["snapshot_et"]).dt.total_seconds() / 60.0
).round(1)
# Prefix the run_key with route when plotting multiple routes so the legend
# is readable and colors don't visually conflate routes.
if ALL_ROUTES:
    predictions["run_key"] = (
        "rt " + predictions["route_short_name"].astype(str)
        + " · " + predictions["trip_id"].astype(str)
        + " · " + predictions["trip_start_date"].astype(str)
    )
else:
    predictions["run_key"] = (
        predictions["trip_id"].astype(str) + " · " + predictions["trip_start_date"].astype(str)
    )
print(
    f"{len(predictions)} predictions across "
    f"{predictions['run_key'].nunique()} trip runs "
    f"({predictions['route_short_name'].nunique()} routes)"
)

# %% [markdown]
# ## 3. Extract observed and scheduled arrivals from the same query
#
# `vw_prediction_error` already LEFT-JOINs `stop_arrival_event` and
# `scheduled_stop_time`, so the observed timestamp and the static-schedule
# arrival_time (seconds since service-day local midnight) ride along on each
# prediction row. Collapse to one row per (trip_id, trip_start_date) and
# do the timezone math in Python — `scheduled_arrival_seconds` may exceed
# 86400 for after-midnight trips, which `Timedelta` handles cleanly.

# %%
trip_first = (
    predictions.dropna(subset=["observed_arrival_ts"])
    .drop_duplicates(["trip_id", "trip_start_date"])
)
observed = trip_first[
    ["trip_id", "trip_start_date", "stop_sequence",
     "observed_arrival_ts", "derivation", "confidence"]
].copy()
observed["observed_et"] = (
    pd.to_datetime(observed["observed_arrival_ts"])
      .dt.tz_localize("UTC")
      .dt.tz_convert(ET)
)
print(f"{len(observed)} observed arrivals matched")

scheduled = (
    predictions.dropna(subset=["scheduled_arrival_seconds"])
    .drop_duplicates(["trip_id", "trip_start_date"])
    [["trip_id", "trip_start_date", "stop_sequence", "scheduled_arrival_seconds"]]
    .copy()
)
scheduled["scheduled_et"] = scheduled.apply(
    lambda r: (
        pd.Timestamp(r["trip_start_date"], tz=ET)
        + pd.Timedelta(seconds=int(r["scheduled_arrival_seconds"]))
    ),
    axis=1,
)
print(f"{len(scheduled)} scheduled arrivals matched")

# Stop name comes back on every row of the view; pluck it once.
stop_name = (
    predictions["stop_name"].dropna().iloc[0]
    if predictions["stop_name"].notna().any()
    else STOP_ID
)

# %% [markdown]
# ## 4. Render

# %%
fig = px.line(
    predictions.sort_values(["run_key", "snapshot_et"]),
    x="snapshot_et",
    y="predicted_et",
    color="run_key",
    markers=True,
    hover_data=["vehicle_id", "stop_sequence", "minutes_out"],
    labels={
        "snapshot_et": "Poll time (ET)",
        "predicted_et": "Predicted arrival (ET)",
        "run_key": "Trip",
    },
    title=(
        f"Prediction evolution — stop {STOP_ID} ({stop_name}), "
        + (
            f"all tracked routes<br>"
            if ALL_ROUTES
            else f"route {ROUTE_SHORT_NAME} dir {DIRECTION_ID}<br>"
        )
        + f"<sub>{WINDOW_START_ET} → {WINDOW_END_ET} ET · "
        f"{predictions['run_key'].nunique()} trips · "
        f"{predictions['route_short_name'].nunique()} routes</sub>"
    ),
)

# Diagonal y = x ("now"). Use the actual data range so the line spans the plot.
diag_lo = min(predictions["snapshot_et"].min(), predictions["predicted_et"].min())
diag_hi = max(predictions["snapshot_et"].max(), predictions["predicted_et"].max())
fig.add_trace(
    go.Scatter(
        x=[diag_lo, diag_hi],
        y=[diag_lo, diag_hi],
        mode="lines",
        line=dict(color="lightgray", width=1, dash="solid"),
        name="now (y = x)",
        hoverinfo="skip",
        showlegend=True,
    )
)

# Per-trip observed (dashed) and scheduled (dotted) horizontals, color-matched
# to the trip's prediction line. Plotly assigns colors via px in the order
# unique values first appear; mirror that.
trip_order = (
    predictions.sort_values(["run_key", "snapshot_et"])["run_key"].drop_duplicates().tolist()
)
palette = px.colors.qualitative.Plotly
color_for = {t: palette[i % len(palette)] for i, t in enumerate(trip_order)}

obs_lookup = {
    (r["trip_id"], r["trip_start_date"]): r["observed_et"]
    for _, r in observed.iterrows()
}
sched_lookup = {
    (r["trip_id"], r["trip_start_date"]): r["scheduled_et"]
    for _, r in scheduled.iterrows()
}
trip_x_range = predictions.groupby("run_key")["snapshot_et"].agg(["min", "max"])

for run_key in trip_order:
    sub = predictions[predictions["run_key"] == run_key].iloc[0]
    key = (sub["trip_id"], sub["trip_start_date"])
    x0, x1 = trip_x_range.loc[run_key, "min"], trip_x_range.loc[run_key, "max"]
    color = color_for[run_key]
    if key in obs_lookup:
        fig.add_trace(
            go.Scatter(
                x=[x0, x1],
                y=[obs_lookup[key], obs_lookup[key]],
                mode="lines",
                line=dict(color=color, width=1, dash="dash"),
                name=f"observed · {run_key}",
                hovertemplate=f"observed arrival: %{{y}}<br>trip {run_key}<extra></extra>",
                showlegend=False,
            )
        )
    if key in sched_lookup:
        fig.add_trace(
            go.Scatter(
                x=[x0, x1],
                y=[sched_lookup[key], sched_lookup[key]],
                mode="lines",
                line=dict(color=color, width=1, dash="dot"),
                name=f"scheduled · {run_key}",
                hovertemplate=f"scheduled arrival: %{{y}}<br>trip {run_key}<extra></extra>",
                showlegend=False,
            )
        )

fig.update_layout(
    legend_title_text="Trip (id · start date)",
    hovermode="closest",
)
fig.show()

# %% [markdown]
# ### Tips
#
# * The legend toggles per-trip prediction lines; the per-trip dashed
#   (observed) and dotted (scheduled) horizontals are intentionally hidden
#   from the legend to keep it readable, but they share each trip's color.
# * If a trip has no dashed line, no `stop_arrival_event` was derived for it
#   yet — usually because the trip is still in progress, was cancelled, or
#   fell into a `gap_no_data` window. See `vw_prediction_error` for the same
#   join exposed as a flat table.
# * To zero in on one bus, click any other entry in the legend to hide it.
