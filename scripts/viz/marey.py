# %% [markdown]
# # Marey (string-line) diagram
#
# The classic transit diagnostic. For a single route + direction:
#
# - **X axis**: time of day
# - **Y axis**: stop sequence along the route (0 = terminal A, N = terminal B)
# - **Each line** = one bus's journey
#
# Bunching looks like two lines running right on top of each other.
# A "hole" (no bus for a long time) looks like an empty horizontal band.
#
# We use the *realtime* `route_id` (e.g. "22"), find a representative trip
# from the static schedule that has the same `route_short_name`, take that
# trip's stop sequence as our spine, and plot every realtime arrival at
# those stops across the window.

# %%
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.io as pio

from db import query

pio.renderers.default = "browser"

# %% [markdown]
# ## Config

# %%
ROUTE_SHORT_NAME = "22"     # e.g. "22", "110", "816"
DIRECTION_ID     = 1        # 0 or 1
LOOKBACK_HOURS   = 24

# %% [markdown]
# ## 1. Pick a canonical stop sequence for the route/direction
#
# MARTA's static feed uses internal route_ids (e.g. 26916) while realtime
# uses human route numbers (e.g. 22). Join via `route_short_name` and grab
# the trip with the most stops as our "spine".

# %%
spine = query(
    """
    WITH candidate_trips AS (
        SELECT TOP 1 t.trip_id
        FROM dbo.trip t
        JOIN dbo.route r ON r.route_id = t.route_id
        JOIN dbo.scheduled_stop_time sst ON sst.trip_id = t.trip_id
        WHERE r.route_short_name = ?
          AND t.direction_id = ?
        GROUP BY t.trip_id
        ORDER BY COUNT(*) DESC
    )
    SELECT sst.stop_sequence, sst.stop_id, s.stop_name
    FROM candidate_trips ct
    JOIN dbo.scheduled_stop_time sst ON sst.trip_id = ct.trip_id
    JOIN dbo.stop s ON s.stop_id = sst.stop_id
    ORDER BY sst.stop_sequence
    """,
    (ROUTE_SHORT_NAME, DIRECTION_ID),
)
if spine.empty:
    raise SystemExit(
        f"No trip found for route_short_name={ROUTE_SHORT_NAME!r} "
        f"direction={DIRECTION_ID}. Try the other direction or a different route."
    )

spine = spine.reset_index(drop=True)
print(f"Spine: {len(spine)} stops on route {ROUTE_SHORT_NAME} dir {DIRECTION_ID}")
spine.head()

# %% [markdown]
# ## 2. Pull every realtime arrival at any of those stops
#
# Filter to the realtime route whose `route_short_name` matches.

# %%
stop_ids = spine["stop_id"].tolist()
placeholders = ",".join(["?"] * len(stop_ids))

arrivals = query(
    f"""
    SELECT
        e.observed_arrival_ts,
        e.stop_id,
        e.route_id,
        e.direction_id,
        e.vehicle_id,
        e.trip_id
    FROM dbo.stop_arrival_event e
    JOIN dbo.route r ON r.route_id = e.route_id
    WHERE r.route_short_name = ?
      AND (e.direction_id = ? OR e.direction_id IS NULL)
      AND e.stop_id IN ({placeholders})
      AND e.observed_arrival_ts > DATEADD(hour, -?, SYSUTCDATETIME())
      AND e.confidence IN ('high','medium')
    """,
    tuple([ROUTE_SHORT_NAME, DIRECTION_ID] + stop_ids + [LOOKBACK_HOURS]),
)
print(f"{len(arrivals)} arrivals fetched")

# %% [markdown]
# ## 3. Glue arrivals to the spine and render

# %%
df = arrivals.merge(spine, on="stop_id", how="inner")
df["arrival_et"] = (
    pd.to_datetime(df["observed_arrival_ts"])
      .dt.tz_localize("UTC")
      .dt.tz_convert("America/New_York")
)
# vehicle_id alone can wrap across service days; group by (vehicle, date)
df["service_date"] = df["arrival_et"].dt.date.astype(str)
df["run_key"] = df["vehicle_id"].astype(str) + " · " + df["trip_id"].astype(str)
df = df.sort_values(["run_key", "arrival_et"])

fig = px.line(
    df,
    x="arrival_et",
    y="stop_sequence",
    color="run_key",
    markers=True,
    hover_data=["stop_name", "vehicle_id", "trip_id", "stop_id"],
    title=f"Marey diagram — route {ROUTE_SHORT_NAME} dir {DIRECTION_ID} "
          f"(last {LOOKBACK_HOURS}h, {df['run_key'].nunique()} runs)",
    labels={"arrival_et": "Time (ET)", "stop_sequence": "Stop sequence"},
)
fig.update_layout(showlegend=False)  # too many runs to legend usefully
# invert Y so terminal-A is at the top, like a classic Marey
fig.update_yaxes(autorange="reversed")
fig.show()

# %% [markdown]
# ### Reading the chart
#
# * Each colored polyline is one bus run. Steeper lines = faster buses.
# * Parallel close lines = bunching. Two runs that "merge" horizontally are
#   the textbook case: the trailing bus catches the leader.
# * A horizontal band of empty space = a service gap (no bus for a while).
# * Lines that flip direction mid-chart usually mean the bus turned around
#   at a terminal and the data captured both legs.
