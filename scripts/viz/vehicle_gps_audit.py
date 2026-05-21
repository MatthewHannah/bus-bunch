# %% [markdown]
# # Vehicle GPS audit
#
# Debug tool for "why is this bus missing from `stop_arrival_event`?".
#
# Pulls every `vehicle_position_snapshot` row for one `vehicle_id` in a time
# window and renders them two ways:
#
# 1. **Per-trip summary** — one row per `(route_short_name, trip_id)` the
#    vehicle was assigned to in the window, with ping count, first/last GPS
#    time, bounding box, and how many derived `stop_arrival_event` rows we
#    have for that trip (joined by `trip_id`, bounded to ±12h around the
#    audit window so cross-day collisions can't sneak in). This is the lens
#    for "trip X had 40 pings but 0 arrivals".
#
# 2. **Raw position table** — every GPS ping in chronological order:
#    snapshot_ts (ET), route, trip, lat/lon, bearing, speed_mph,
#    veh_ts (the *device's* clock — useful for spotting stale GPS), and a
#    `gap_to_prev_s` column so big silences jump out.
#
# Both render in the browser as scrollable, sortable Plotly tables. The raw
# table is also written to a CSV next to the HTML for grep/jq/Excel work.

# %%
from __future__ import annotations

import os
import tempfile
import webbrowser

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

from db import query

pio.renderers.default = "browser"

ET = "America/New_York"
MPS_TO_MPH = 2.2369363

# %% [markdown]
# ## Config

# %%
VEHICLE_ID       = "1525"                 # MARTA vehicle_id
WINDOW_START_ET  = "2026-05-13 04:00"     # local (Eastern)
WINDOW_END_ET    = "2026-05-13 09:00"

# %% [markdown]
# ## 1. Pull every GPS ping in the window

# %%
window_start_utc = pd.Timestamp(WINDOW_START_ET, tz=ET).tz_convert("UTC").tz_localize(None)
window_end_utc   = pd.Timestamp(WINDOW_END_ET,   tz=ET).tz_convert("UTC").tz_localize(None)

pings = query(
    """
    SELECT
        v.snapshot_ts,
        v.veh_ts,
        v.vehicle_id,
        v.route_id,
        r.route_short_name,
        v.direction_id,
        v.trip_id,
        v.latitude,
        v.longitude,
        v.bearing,
        v.speed_mps
    FROM dbo.vehicle_position_snapshot v
    LEFT JOIN dbo.route r ON r.route_id = v.route_id
    WHERE v.vehicle_id = ?
      AND v.snapshot_ts >= ?
      AND v.snapshot_ts <  ?
    ORDER BY v.snapshot_ts
    """,
    (
        VEHICLE_ID,
        window_start_utc.to_pydatetime(),
        window_end_utc.to_pydatetime(),
    ),
)
if pings.empty:
    raise SystemExit(
        f"No GPS pings for vehicle_id={VEHICLE_ID!r} between "
        f"{WINDOW_START_ET} and {WINDOW_END_ET} ET."
    )

pings["snapshot_et"] = (
    pd.to_datetime(pings["snapshot_ts"]).dt.tz_localize("UTC").dt.tz_convert(ET)
)
# Build veh_et all at once so the column is tz-aware from creation. Naive
# rows become NaT cleanly via to_datetime; tz-localize keeps NaT as NaT.
pings["veh_et"] = (
    pd.to_datetime(pings["veh_ts"], errors="coerce")
      .dt.tz_localize("UTC")
      .dt.tz_convert(ET)
)
pings["latitude"]  = pings["latitude"].astype(float)
pings["longitude"] = pings["longitude"].astype(float)
pings["speed_mph"] = (pings["speed_mps"].astype(float) * MPS_TO_MPH).round(1)
pings["gap_to_prev_s"] = (
    pings["snapshot_et"].diff().dt.total_seconds().fillna(0).astype(int)
)
# veh_lag = how stale the device's own timestamp was at the moment we polled.
pings["veh_lag_s"] = (
    (pings["snapshot_et"] - pings["veh_et"]).dt.total_seconds()
)

# Replace nulls with display-friendly sentinels for the table
pings_display = pings.assign(
    route_short_name=pings["route_short_name"].fillna("—"),
    direction_id=pings["direction_id"].astype("Int64").astype(str).replace("<NA>", "—"),
    trip_id=pings["trip_id"].fillna("—"),
    bearing=pings["bearing"].astype("Int64").astype(str).replace("<NA>", "—"),
)

print(
    f"{len(pings)} pings for vehicle {VEHICLE_ID} between "
    f"{WINDOW_START_ET} and {WINDOW_END_ET} ET"
)

# %% [markdown]
# ## 2. Per-trip summary, joined to `stop_arrival_event`
#
# `vehicle_position_snapshot` doesn't carry `trip_start_date`, so we key by
# `trip_id` alone for grouping. To avoid picking up arrivals from a previous
# service day that happened to reuse a trip_id, we bound the arrival lookup
# to ±12h around the audit window (much wider than any one trip).

# %%
trips_in_window = (
    pings.dropna(subset=["trip_id"])
        .groupby(["route_id", "route_short_name", "direction_id", "trip_id"], dropna=False)
        .agg(
            pings=("snapshot_ts", "count"),
            first_ping_et=("snapshot_et", "min"),
            last_ping_et=("snapshot_et", "max"),
            min_lat=("latitude", "min"),
            max_lat=("latitude", "max"),
            min_lon=("longitude", "min"),
            max_lon=("longitude", "max"),
            max_gap_s=("gap_to_prev_s", "max"),
        )
        .reset_index()
)

# Also surface pings with no trip assigned at all (vehicle "off route" /
# between trips). These can't be joined to arrivals but are useful context.
no_trip_pings = pings[pings["trip_id"].isna()]
if not no_trip_pings.empty:
    print(f"  ⚠ {len(no_trip_pings)} pings had NULL trip_id (off route / between trips)")

if trips_in_window.empty:
    print("  ⚠ No assigned trips in this window — the bus was idle the whole time.")
    arrivals_per_trip = pd.DataFrame(columns=["trip_id", "arrivals"])
else:
    trip_ids = trips_in_window["trip_id"].drop_duplicates().tolist()
    placeholders = ",".join(["?"] * len(trip_ids))
    arr_window_start = window_start_utc - pd.Timedelta(hours=12)
    arr_window_end   = window_end_utc   + pd.Timedelta(hours=12)
    arrivals = query(
        f"""
        SELECT trip_id, trip_start_date, COUNT(*) AS arrivals,
               MIN(observed_arrival_ts) AS first_arrival_ts,
               MAX(observed_arrival_ts) AS last_arrival_ts,
               MIN(stop_sequence) AS min_seq,
               MAX(stop_sequence) AS max_seq
        FROM dbo.stop_arrival_event
        WHERE trip_id IN ({placeholders})
          AND observed_arrival_ts >= ?
          AND observed_arrival_ts <  ?
        GROUP BY trip_id, trip_start_date
        """,
        tuple(trip_ids + [arr_window_start.to_pydatetime(),
                          arr_window_end.to_pydatetime()]),
    )
    if not arrivals.empty:
        arrivals["first_arrival_et"] = (
            pd.to_datetime(arrivals["first_arrival_ts"])
              .dt.tz_localize("UTC").dt.tz_convert(ET)
        )
        arrivals["last_arrival_et"] = (
            pd.to_datetime(arrivals["last_arrival_ts"])
              .dt.tz_localize("UTC").dt.tz_convert(ET)
        )
        # Collapse to one row per trip_id in case a trip spans service days.
        arrivals_per_trip = (
            arrivals.groupby("trip_id", as_index=False)
            .agg(
                arrivals=("arrivals", "sum"),
                first_arrival_et=("first_arrival_et", "min"),
                last_arrival_et=("last_arrival_et", "max"),
                min_seq=("min_seq", "min"),
                max_seq=("max_seq", "max"),
                trip_start_dates=("trip_start_date",
                                  lambda s: ",".join(sorted(set(s)))),
            )
        )
    else:
        arrivals_per_trip = pd.DataFrame(columns=[
            "trip_id", "arrivals", "first_arrival_et", "last_arrival_et",
            "min_seq", "max_seq", "trip_start_dates",
        ])

trip_summary = trips_in_window.merge(
    arrivals_per_trip,
    on="trip_id",
    how="left",
)
trip_summary["arrivals"] = trip_summary["arrivals"].fillna(0).astype(int)
trip_summary = trip_summary.sort_values(["first_ping_et"]).reset_index(drop=True)

# Highlight zero-arrival trips with a sentinel column for the table.
trip_summary["status"] = trip_summary["arrivals"].apply(
    lambda n: "⚠ no arrivals" if n == 0 else f"{n} arrivals"
)
print(f"{len(trip_summary)} distinct trips in window; "
      f"{(trip_summary['arrivals'] == 0).sum()} have zero derived arrivals")

# %% [markdown]
# ## 3. Render two Plotly tables in one HTML page
#
# Plotly's `Table` traces are dirt simple — header + columns. We stack two
# in one figure with `domain` so the summary sits above the raw pings.

# %%

def _fmt_et(s: pd.Series) -> list[str]:
    return [v.strftime("%Y-%m-%d %H:%M:%S") if pd.notna(v) else "—" for v in s]

summary_cols = [
    ("route", trip_summary["route_short_name"].fillna(trip_summary["route_id"]).astype(str).tolist()),
    ("dir",   trip_summary["direction_id"].astype("Int64").astype(str).str.replace("<NA>", "—").tolist()),
    ("trip_id", trip_summary["trip_id"].astype(str).tolist()),
    ("start_date(s)",
     trip_summary.get("trip_start_dates",
                      pd.Series(["—"] * len(trip_summary))).fillna("—").astype(str).tolist()),
    ("pings", trip_summary["pings"].astype(int).tolist()),
    ("first_ping (ET)", _fmt_et(trip_summary["first_ping_et"])),
    ("last_ping (ET)",  _fmt_et(trip_summary["last_ping_et"])),
    ("max gap (s)", trip_summary["max_gap_s"].astype(int).tolist()),
    ("bbox lat",  [f"{lo:.4f}–{hi:.4f}" for lo, hi in zip(trip_summary["min_lat"], trip_summary["max_lat"])]),
    ("bbox lon",  [f"{lo:.4f}–{hi:.4f}" for lo, hi in zip(trip_summary["min_lon"], trip_summary["max_lon"])]),
    ("arrivals", trip_summary["status"].tolist()),
    ("arrival seq", [
        "—" if pd.isna(a) or pd.isna(b) else f"{int(a)}–{int(b)}"
        for a, b in zip(trip_summary.get("min_seq", []), trip_summary.get("max_seq", []))
    ] if "min_seq" in trip_summary else ["—"] * len(trip_summary)),
]
# Color the "arrivals" cell red when zero so the eye snaps to it.
arrival_fill = ["#ffe5e5" if n == 0 else "#ffffff" for n in trip_summary["arrivals"]]
fill_colors = [
    ["#ffffff"] * len(trip_summary) for _ in range(len(summary_cols) - 2)
] + [arrival_fill, ["#ffffff"] * len(trip_summary)]

raw_cols = [
    ("snapshot (ET)", _fmt_et(pings["snapshot_et"])),
    ("gap (s)",       pings["gap_to_prev_s"].astype(int).tolist()),
    ("route",         pings_display["route_short_name"].astype(str).tolist()),
    ("dir",           pings_display["direction_id"].astype(str).tolist()),
    ("trip_id",       pings_display["trip_id"].astype(str).tolist()),
    ("lat",           pings["latitude"].round(5).astype(str).tolist()),
    ("lon",           pings["longitude"].round(5).astype(str).tolist()),
    ("bearing",       pings_display["bearing"].astype(str).tolist()),
    ("mph",           pings["speed_mph"].astype(str).tolist()),
    ("veh_ts (ET)",   _fmt_et(pings["veh_et"])),
    ("veh_lag (s)",   [
        "—" if pd.isna(v) else str(int(v))
        for v in pings["veh_lag_s"]
    ]),
]
# Highlight pings with no trip assigned.
no_trip_mask = pings["trip_id"].isna().tolist()
trip_fill = ["#ffe5e5" if x else "#ffffff" for x in no_trip_mask]
raw_fill = [
    trip_fill if header == "trip_id" else ["#ffffff"] * len(pings)
    for header, _ in raw_cols
]

fig = go.Figure(data=[
    go.Table(
        domain=dict(x=[0, 1], y=[0.55, 1]),
        header=dict(
            values=[f"<b>{h}</b>" for h, _ in summary_cols],
            fill_color="#dee5ef",
            align="left",
        ),
        cells=dict(
            values=[c for _, c in summary_cols],
            fill_color=fill_colors,
            align="left",
            height=22,
        ),
    ),
    go.Table(
        domain=dict(x=[0, 1], y=[0, 0.5]),
        header=dict(
            values=[f"<b>{h}</b>" for h, _ in raw_cols],
            fill_color="#dee5ef",
            align="left",
        ),
        cells=dict(
            values=[c for _, c in raw_cols],
            fill_color=raw_fill,
            align="left",
            height=20,
        ),
    ),
])
fig.update_layout(
    title=(
        f"Vehicle {VEHICLE_ID} GPS audit · "
        f"{WINDOW_START_ET} → {WINDOW_END_ET} ET<br>"
        f"<sub>{len(pings)} pings · {len(trip_summary)} trips · "
        f"{(trip_summary['arrivals'] == 0).sum()} trips with no derived arrivals</sub>"
    ),
    margin=dict(l=10, r=10, t=60, b=10),
    height=900,
    annotations=[
        dict(text="<b>Per-trip summary</b> (red = no arrivals derived)",
             xref="paper", yref="paper", x=0, y=1.02,
             showarrow=False, align="left"),
        dict(text="<b>Raw GPS pings</b> (red trip_id = vehicle had no trip assigned)",
             xref="paper", yref="paper", x=0, y=0.52,
             showarrow=False, align="left"),
    ],
)

out_html = os.path.join(
    tempfile.gettempdir(),
    f"vehicle_gps_{VEHICLE_ID}_"
    f"{pd.Timestamp(WINDOW_START_ET).strftime('%Y%m%d_%H%M')}.html",
)
fig.write_html(out_html, include_plotlyjs="cdn")
webbrowser.open("file://" + out_html)
print(f"Wrote {out_html}")

# %% [markdown]
# ## 4. Also dump the raw pings to CSV
#
# Easier for grep/jq/Excel triage. Sits next to the HTML so it's findable.

# %%
csv_path = out_html.removesuffix(".html") + ".csv"
pings[[
    "snapshot_et", "gap_to_prev_s", "route_id", "route_short_name",
    "direction_id", "trip_id",
    "latitude", "longitude", "bearing", "speed_mph",
    "veh_et", "veh_lag_s",
]].to_csv(csv_path, index=False)
print(f"Wrote {csv_path}")

# %% [markdown]
# ### Reading the output
#
# * **Top table — per trip.** A row with `pings = 40, arrivals = 0` is the
#   smoking gun. Common causes:
#     * the trip never matched a static `scheduled_stop_time` row
#       (mid-shakeup GTFS update, trip_id changed)
#     * `confidence` of derived events was downgraded below the cutoff
#     * the trip fell into a `gap_no_data` / `mass_dropout` window —
#       cross-check `dbo.snapshot.skipped_reason` for the same time range
#     * the bus was reassigned mid-route (vehicle_id flipped to a new trip)
#       so individual stops never made it into `trip_stop_prediction` long
#       enough to be diff-derived
# * **Bottom table — raw pings.** Big `gap (s)` jumps are missing polls
#   (Functions cold start, MARTA outage). A red trip_id row means the bus
#   was reporting GPS but unassigned — usually layover or out-of-service.
# * **`veh_lag (s)`**: how stale the bus's own timestamp was at poll time.
#   Spikes >120s suggest the AVL itself is dropping out, even though we got
#   *some* row back.
# * The CSV next to the HTML has the same raw rows for ad-hoc analysis.
