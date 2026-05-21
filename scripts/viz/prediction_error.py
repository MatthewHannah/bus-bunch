# %% [markdown]
# # Prediction *error* at a stop (drift vs. ground truth)
#
# Third companion to `prediction_evolution.py` / `prediction_drift.py`. Same
# data source, but anchor the Y axis on the *observed* arrival instead of the
# first prediction.
#
# - **X axis**: `snapshot_ts` (ET)
# - **Y axis**: `predicted_arrival_ts - observed_arrival_ts` for that trip,
#   in **minutes**. By definition this **converges to 0 as the bus arrives**.
#     * Positive = MARTA over-predicted (said the bus would come later than
#       it actually did → bus came early relative to the ETA).
#     * Negative = MARTA under-predicted (bus came late vs. the ETA).
# - **One colored line** per `(trip_id, trip_start_date)`.
# - **Dotted gray** at y = 0 = perfect prediction reference.
# - **Color-matched `×`** marker on the zero line at each trip's observed
#   arrival time — the moment "now" caught up with the prediction.
#
# Only trips with a derived `stop_arrival_event` can be plotted (we need
# ground truth). Trips still in progress / cancelled / lost to a data gap
# are reported in the console output and skipped.
#
# This is essentially a per-trip view of `dbo.vw_prediction_error.error_seconds`
# (we use that view directly for the join).

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
WINDOW_START_ET  = "2026-05-19 06:00"     # local (Eastern)
WINDOW_END_ET    = "2026-05-19 12:00"

# Drop predictions whose horizon is huge (e.g. 60+ min away). They dominate
# the Y axis and aren't usually what you want to look at. Set to None to keep
# everything.
MAX_HORIZON_MIN  = 30

ALL_ROUTES = not ROUTE_SHORT_NAME

# %% [markdown]
# ## 1. Sanity-check tracked routes

# %%
tracked = query(
    """
    SELECT tr.route_id, r.route_short_name
    FROM dbo.tracked_route tr
    LEFT JOIN dbo.route r ON r.route_id = tr.route_id
    """
)
tracked_short_names = sorted(tracked["route_short_name"].dropna().astype(str).tolist())
if ALL_ROUTES:
    if not tracked_short_names:
        raise SystemExit(
            "dbo.tracked_route is empty, so no predictions are being recorded."
        )
elif ROUTE_SHORT_NAME not in set(tracked_short_names):
    raise SystemExit(
        f"Route {ROUTE_SHORT_NAME!r} is not in dbo.tracked_route. "
        f"Currently tracked: {tracked_short_names}"
    )

# %% [markdown]
# ## 2. Pull predictions joined to observed arrivals
#
# `vw_prediction_error` already does the LEFT JOIN to `stop_arrival_event`
# and exposes `error_seconds`. We restrict to `arrival_resolution = 'matched'`
# so we only plot trips whose ground truth is real (excludes 'unresolved' and
# the lower-confidence 'suspect_cancelled').

# %%
window_start_utc = pd.Timestamp(WINDOW_START_ET, tz=ET).tz_convert("UTC").tz_localize(None)
window_end_utc   = pd.Timestamp(WINDOW_END_ET,   tz=ET).tz_convert("UTC").tz_localize(None)

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
        v.error_seconds,
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

raw = query(base_sql, tuple(params))
if raw.empty:
    scope = "any tracked route" if ALL_ROUTES else f"route {ROUTE_SHORT_NAME} dir {DIRECTION_ID}"
    raise SystemExit(
        f"No predictions found for stop {STOP_ID} on {scope} between "
        f"{WINDOW_START_ET} and {WINDOW_END_ET} ET."
    )

# Report and drop unresolved trips up-front so the user knows what's missing.
resolution_counts = raw["arrival_resolution"].value_counts().to_dict()
print(f"Resolution mix: {resolution_counts}")
predictions = raw[raw["arrival_resolution"] == "matched"].copy()
if predictions.empty:
    raise SystemExit(
        "No 'matched' predictions in the window — every trip is unresolved or "
        "suspect_cancelled, so we have no ground truth to compute error against."
    )

# %% [markdown]
# ## 3. Compute error and tidy

# %%
predictions["snapshot_et"] = (
    pd.to_datetime(predictions["snapshot_ts"]).dt.tz_localize("UTC").dt.tz_convert(ET)
)
predictions["predicted_et"] = (
    pd.to_datetime(predictions["predicted_arrival_ts"])
      .dt.tz_localize("UTC")
      .dt.tz_convert(ET)
)
predictions["observed_et"] = (
    pd.to_datetime(predictions["observed_arrival_ts"])
      .dt.tz_localize("UTC")
      .dt.tz_convert(ET)
)
predictions["error_min"]   = predictions["error_seconds"] / 60.0
predictions["horizon_min"] = (
    (predictions["observed_et"] - predictions["snapshot_et"]).dt.total_seconds() / 60.0
)
# Drop predictions emitted *after* the observed arrival (negative horizon) —
# those are post-hoc rows that occasionally appear in the feed and just add
# noise to the right edge.
predictions = predictions[predictions["horizon_min"] >= 0]
if MAX_HORIZON_MIN is not None:
    predictions = predictions[predictions["horizon_min"] <= MAX_HORIZON_MIN]

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
predictions = predictions.sort_values(["run_key", "snapshot_et"]).reset_index(drop=True)

print(
    f"{len(predictions)} matched predictions across "
    f"{predictions['run_key'].nunique()} trip runs "
    f"({predictions['route_short_name'].nunique()} routes)"
)

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
    predictions,
    x="snapshot_et",
    y="error_min",
    color="run_key",
    markers=True,
    hover_data=["vehicle_id", "predicted_et", "observed_et", "horizon_min"],
    labels={
        "snapshot_et": "Poll time (ET)",
        "error_min":   "Predicted − Observed (minutes)",
        "run_key":     "Trip",
    },
    title=(
        f"Prediction error vs. ground truth — stop {STOP_ID} ({stop_name}), "
        + (
            f"all tracked routes<br>"
            if ALL_ROUTES
            else f"route {ROUTE_SHORT_NAME} dir {DIRECTION_ID}<br>"
        )
        + f"<sub>{WINDOW_START_ET} → {WINDOW_END_ET} ET · "
        f"{predictions['run_key'].nunique()} trips · "
        f"{predictions['route_short_name'].nunique()} routes"
        + (f" · horizon ≤ {MAX_HORIZON_MIN} min" if MAX_HORIZON_MIN is not None else "")
        + "</sub>"
    ),
)

# Zero-error reference.
x_lo = predictions["snapshot_et"].min()
x_hi = max(predictions["snapshot_et"].max(), predictions["observed_et"].max())
fig.add_trace(
    go.Scatter(
        x=[x_lo, x_hi], y=[0, 0],
        mode="lines",
        line=dict(color="lightgray", width=1, dash="dot"),
        name="perfect (y = 0)",
        hoverinfo="skip",
        showlegend=True,
    )
)

# Per-trip arrival marker on the zero line, anchored at the observed arrival
# time on the X axis.
trip_order = predictions["run_key"].drop_duplicates().tolist()
palette = px.colors.qualitative.Plotly
color_for = {t: palette[i % len(palette)] for i, t in enumerate(trip_order)}

trip_meta = (
    predictions.groupby("run_key")
    .agg(observed_et=("observed_et", "first"))
    .reset_index()
)
for _, m in trip_meta.iterrows():
    fig.add_trace(
        go.Scatter(
            x=[m["observed_et"]],
            y=[0],
            mode="markers",
            marker=dict(symbol="x", size=10, color=color_for[m["run_key"]]),
            name=f"arrived · {m['run_key']}",
            hovertemplate=(
                f"arrived: %{{x}}<br>trip {m['run_key']}<extra></extra>"
            ),
            showlegend=False,
        )
    )

fig.update_layout(legend_title_text="Trip", hovermode="closest")
fig.show()

# %% [markdown]
# ### Tips
#
# * Every line should taper toward 0 as `snapshot_ts` approaches the bus's
#   actual arrival. A line that *never* gets to 0 means MARTA's last
#   prediction before the bus arrived was still off — useful for accuracy
#   audits.
# * Sustained positive plateau = MARTA chronically over-predicting (the bus
#   keeps showing up earlier than promised). Sustained negative = chronically
#   under-predicting (riders consistently miss the bus).
# * `MAX_HORIZON_MIN` trims far-future predictions whose error swings tend to
#   dwarf the near-arrival behavior. Set it to `None` to see the full series.
# * If many trips show as `unresolved` in the console output, derivation may
#   still be in progress — re-run later, or widen the window so trips have
#   time to complete.
