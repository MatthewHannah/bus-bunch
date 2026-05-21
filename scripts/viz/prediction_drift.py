# %% [markdown]
# # Prediction *drift* at a stop
#
# Companion to `prediction_evolution.py`. Same data, different lens: instead
# of plotting absolute predicted arrival times, plot how much each trip's ETA
# at one stop has *moved* since the very first prediction we saw for it.
#
# - **X axis**: `snapshot_ts` (ET)
# - **Y axis**: `predicted_arrival_ts - first_predicted_arrival_ts` for that
#   trip, in **minutes**. Always starts at 0 by definition.
# - **One colored line** per `(trip_id, trip_start_date)`
# - **Per trip, color-matched dashed marker** at the *final drift* — i.e.
#   `observed_arrival_ts - first_predicted_arrival_ts` — drawn as a short
#   horizontal stub at the trip's last snapshot. That's "how wrong the very
#   first ETA turned out to be".
# - **Dotted horizontal at y = 0**: no drift reference.
#
# Reading it:
#   * Line stays near 0 → MARTA's initial estimate held up.
#   * Line drifts up → ETA kept slipping later (bus losing time).
#   * Line drifts down → bus running ahead of the first guess.
#   * Late vertical jumps → MARTA suddenly revised the ETA (e.g. picked up a
#     trip on a new vehicle, traffic event).
#   * Two trips drifting toward the same final dashed marker = bunching
#     forming.
#
# Same `dbo.tracked_route` requirement and same "" / None = all-routes mode
# as `prediction_evolution.py`.

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
ROUTE_SHORT_NAME = ""                     # realtime/public route number; "" or None = ALL routes serving this stop
DIRECTION_ID     = 0                      # 0 or 1; ignored when ROUTE_SHORT_NAME is empty
WINDOW_START_ET  = "2026-05-12 08:00"     # local (Eastern)
WINDOW_END_ET    = "2026-05-12 12:00"

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
# ## 2. Pull the prediction trajectories
#
# Same filters and window semantics as `prediction_evolution.py`. We read
# `vw_prediction_error` so the observed arrival used for "final drift" rides
# along with the prediction rows; we only diverge in how we transform.

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
        v.observed_arrival_ts
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
        f"{WINDOW_START_ET} and {WINDOW_END_ET} ET."
    )

predictions["snapshot_et"] = (
    pd.to_datetime(predictions["snapshot_ts"]).dt.tz_localize("UTC").dt.tz_convert(ET)
)
predictions["predicted_et"] = (
    pd.to_datetime(predictions["predicted_arrival_ts"])
      .dt.tz_localize("UTC")
      .dt.tz_convert(ET)
)
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

# %% [markdown]
# ## 3. Compute drift per trip
#
# `first_predicted_et` per `run_key` (chronologically first snapshot's ETA)
# is the baseline. `drift_min = (predicted_et - first_predicted_et)` in
# minutes — positive means MARTA pushed the ETA later.

# %%
predictions = predictions.sort_values(["run_key", "snapshot_et"]).reset_index(drop=True)
firsts = (
    predictions.groupby("run_key", as_index=False)
    .first()[["run_key", "predicted_et", "snapshot_et"]]
    .rename(columns={
        "predicted_et": "first_predicted_et",
        "snapshot_et":  "first_snapshot_et",
    })
)
predictions = predictions.merge(firsts, on="run_key", how="left")
predictions["drift_min"] = (
    (predictions["predicted_et"] - predictions["first_predicted_et"])
    .dt.total_seconds() / 60.0
).round(2)

print(
    f"{len(predictions)} predictions across "
    f"{predictions['run_key'].nunique()} trip runs "
    f"({predictions['route_short_name'].nunique()} routes)"
)

# %% [markdown]
# ## 4. Extract observed arrivals for the final-drift markers
#
# `vw_prediction_error` LEFT-JOINs `stop_arrival_event`, so the observed
# timestamp is already on each prediction row. Collapse to one row per
# (trip_id, trip_start_date) and stop name comes off the same dataframe.

# %%
observed = (
    predictions.dropna(subset=["observed_arrival_ts"])
    .drop_duplicates(["trip_id", "trip_start_date"])
    [["trip_id", "trip_start_date", "stop_sequence", "observed_arrival_ts"]]
    .copy()
)
observed["observed_et"] = (
    pd.to_datetime(observed["observed_arrival_ts"])
      .dt.tz_localize("UTC")
      .dt.tz_convert(ET)
)

stop_name = (
    predictions["stop_name"].dropna().iloc[0]
    if predictions["stop_name"].notna().any()
    else STOP_ID
)

# %% [markdown]
# ## 5. Render

# %%
fig = px.line(
    predictions,
    x="snapshot_et",
    y="drift_min",
    color="run_key",
    markers=True,
    hover_data=["vehicle_id", "predicted_et", "first_predicted_et"],
    labels={
        "snapshot_et": "Poll time (ET)",
        "drift_min":   "Drift from first ETA (minutes)",
        "run_key":     "Trip",
    },
    title=(
        f"Prediction drift — stop {STOP_ID} ({stop_name}), "
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

# Zero-drift reference.
x_lo = predictions["snapshot_et"].min()
x_hi = predictions["snapshot_et"].max()
fig.add_trace(
    go.Scatter(
        x=[x_lo, x_hi], y=[0, 0],
        mode="lines",
        line=dict(color="lightgray", width=1, dash="dot"),
        name="no drift (y = 0)",
        hoverinfo="skip",
        showlegend=True,
    )
)

# Per-trip *final drift* marker (color-matched). Plotly assigns colors in the
# order unique color values first appear; mirror that.
trip_order = predictions["run_key"].drop_duplicates().tolist()
palette = px.colors.qualitative.Plotly
color_for = {t: palette[i % len(palette)] for i, t in enumerate(trip_order)}

trip_meta = (
    predictions.groupby("run_key")
    .agg(
        trip_id=("trip_id", "first"),
        trip_start_date=("trip_start_date", "first"),
        first_predicted_et=("first_predicted_et", "first"),
        last_snapshot_et=("snapshot_et", "max"),
    )
    .reset_index()
)
obs_lookup = {
    (r["trip_id"], r["trip_start_date"]): r["observed_et"]
    for _, r in observed.iterrows()
}
for _, m in trip_meta.iterrows():
    key = (m["trip_id"], m["trip_start_date"])
    if key not in obs_lookup:
        continue
    final_drift = (obs_lookup[key] - m["first_predicted_et"]).total_seconds() / 60.0
    # Short horizontal stub from the last snapshot out to the observed arrival
    # time, so the marker is visually anchored to "when the bus actually
    # arrived" on the X axis.
    fig.add_trace(
        go.Scatter(
            x=[m["last_snapshot_et"], obs_lookup[key]],
            y=[final_drift, final_drift],
            mode="lines+markers",
            line=dict(color=color_for[m["run_key"]], width=1, dash="dash"),
            marker=dict(symbol="x", size=9, color=color_for[m["run_key"]]),
            name=f"final · {m['run_key']}",
            hovertemplate=(
                f"final drift: {final_drift:+.2f} min<br>"
                f"observed: %{{x}}<br>trip {m['run_key']}<extra></extra>"
            ),
            showlegend=False,
        )
    )

fig.update_layout(legend_title_text="Trip", hovermode="closest")
fig.show()

# %% [markdown]
# ### Tips
#
# * The Y axis unit is minutes of drift. A trip whose line ends at +5 means
#   MARTA's first guess was 5 minutes too optimistic by the time the bus
#   actually showed up.
# * The dashed `×` marker sits at `(observed_arrival_ts, final_drift)` so
#   you can read both the *magnitude* of the final error and *when* the bus
#   finally arrived.
# * If a trip has no `×` marker, no `stop_arrival_event` was derived for it
#   (still in progress, cancelled, gap_no_data, etc.).
# * Pair with `prediction_evolution.py` when you want to see absolute ETAs
#   alongside drift.
