# %% [markdown]
# # Diagnose a "vanishing" trip on a Marey diagram
#
# Pulls together every angle on a single trip/vehicle to figure out *why* its
# Marey line ends mid-route:
#
# 1. **Schedule** — what stops should this trip serve?
# 2. **Arrivals emitted** — what did `stop_arrival_event` actually capture?
# 3. **Confidence breakdown** — were the missing stops emitted at low
#    confidence (and thus filtered by the Marey)?
# 4. **Poll health** — were we down or skipping derives during the window?
# 5. **GPS trail** — did vehicle 1614 actually keep driving the route after
#    the last emitted arrival? (this is the smoking gun: if the bus is still
#    moving along the line but no arrivals are emitted, MARTA's tripupdates
#    feed dropped the trip; if the bus parked/vanished, it's a real end.)
#
# Defaults to vehicle 1614 / trip 10786584.

# %%
from __future__ import annotations

import math

import pandas as pd

from db import query

pd.set_option("display.max_rows", 200)
pd.set_option("display.width", 200)

# %% [markdown]
# ## Config

# %%
TRIP_ID    = "10784697"
VEHICLE_ID = "1567"
# How far around the trip's emitted-arrival window to inspect snapshots & GPS.
WINDOW_PAD_MIN = 30

# %% [markdown]
# ## 1. What does the static schedule say this trip looks like?

# %%
sched = query(
    """
    SELECT sst.stop_sequence,
           sst.stop_id,
           s.stop_name,
           sst.arrival_time  AS sched_arrival_time,
           sst.departure_time AS sched_departure_time
    FROM dbo.scheduled_stop_time sst
    JOIN dbo.stop s ON s.stop_id = sst.stop_id
    WHERE sst.trip_id = ?
    ORDER BY sst.stop_sequence
    """,
    (TRIP_ID,),
)
print(f"Schedule has {len(sched)} stops; "
      f"sequence range {sched['stop_sequence'].min()}..{sched['stop_sequence'].max()}")
sched.head(3), sched.tail(3)

# %% [markdown]
# ## 2. What arrivals did we actually emit for this trip?

# %%
arrivals = query(
    """
    SELECT
        e.stop_sequence,
        e.stop_id,
        s.stop_name,
        e.observed_arrival_ts,
        e.last_predicted_arrival_ts,
        e.derivation,
        e.confidence,
        e.derived_from_snapshot_ts,
        e.vehicle_id,
        e.trip_start_date
    FROM dbo.stop_arrival_event e
    LEFT JOIN dbo.stop s ON s.stop_id = e.stop_id
    WHERE e.trip_id = ?
    ORDER BY e.stop_sequence
    """,
    (TRIP_ID,),
)
print(f"{len(arrivals)} arrival events emitted for trip {TRIP_ID}")
print(f"  vehicle_ids on emitted events : {arrivals['vehicle_id'].dropna().unique().tolist()}")
print(f"  trip_start_dates on events    : {arrivals['trip_start_date'].dropna().unique().tolist()}")
print(f"  emitted sequence range        : "
      f"{arrivals['stop_sequence'].min()}..{arrivals['stop_sequence'].max()}")
print(f"  confidence breakdown          : {arrivals['confidence'].value_counts().to_dict()}")
print(f"  derivation breakdown          : {arrivals['derivation'].value_counts().to_dict()}")
arrivals

# %% [markdown]
# ## 3. Compare schedule vs emitted — exactly which stops are missing?

# %%
joined = sched.merge(
    arrivals[["stop_sequence", "observed_arrival_ts", "confidence",
              "derivation", "last_predicted_arrival_ts"]],
    on="stop_sequence", how="left",
)
joined["status"] = joined["observed_arrival_ts"].isna().map(
    {True: "MISSING", False: "emitted"}
)
print("Schedule vs emitted (✓=emitted):")
print(joined[["stop_sequence", "stop_name", "sched_arrival_time",
              "status", "confidence", "derivation",
              "observed_arrival_ts", "last_predicted_arrival_ts"]]
      .to_string(index=False))

missing_head = joined[joined["status"] == "MISSING"].head(1)
missing_tail = joined[joined["status"] == "MISSING"].tail(1)
print()
print(f"Missing stops at head : {(joined['status'] == 'MISSING').cumsum().eq((joined.index + 1)).sum()}")
print(f"Total missing         : {(joined['status'] == 'MISSING').sum()}")

# %% [markdown]
# ## 4. Was the system healthy during the window?
#
# Bracket from a bit before the first emitted arrival to a bit after the
# trip's *scheduled* end (so we catch outages that would have killed the
# tail).

# %%
if arrivals.empty:
    raise SystemExit("No arrivals at all for this trip — can't anchor a window.")

first_obs = pd.to_datetime(arrivals["observed_arrival_ts"].min())
last_obs  = pd.to_datetime(arrivals["observed_arrival_ts"].max())
# Pad both ends; pad the tail more (~the schedule's remaining duration).
window_start = first_obs - pd.Timedelta(minutes=WINDOW_PAD_MIN)
window_end   = last_obs + pd.Timedelta(minutes=WINDOW_PAD_MIN + 60)

print(f"First emitted arrival : {first_obs}")
print(f"Last  emitted arrival : {last_obs}")
print(f"Snapshot window       : {window_start}  →  {window_end}")

snaps = query(
    """
    SELECT snapshot_ts,
           feed_ts,
           DATEDIFF(SECOND, LAG(snapshot_ts) OVER (ORDER BY snapshot_ts), snapshot_ts) AS gap_s,
           vehicle_entity_n,
           trip_entity_n,
           upsert_n,
           disappeared_n,
           derived_n,
           skipped_reason
    FROM dbo.snapshot
    WHERE snapshot_ts BETWEEN ? AND ?
    ORDER BY snapshot_ts
    """,
    (window_start.to_pydatetime(), window_end.to_pydatetime()),
)
print(f"\n{len(snaps)} snapshots in window")
print(f"  skipped polls         : {(snaps['skipped_reason'].notna()).sum()}")
print(f"  skipped reasons       : {snaps['skipped_reason'].dropna().value_counts().to_dict()}")
print(f"  max gap_s             : {snaps['gap_s'].max()}")
print(f"  median gap_s          : {snaps['gap_s'].median()}")

# Highlight any snapshot near the LAST emitted arrival — that's where the
# trip would have been killed if it died from a feed glitch.
near_death = snaps[
    (snaps["snapshot_ts"] >= last_obs - pd.Timedelta(minutes=2))
    & (snaps["snapshot_ts"] <= last_obs + pd.Timedelta(minutes=10))
]
print(f"\nSnapshots within [-2, +10] min of last emitted arrival "
      f"({last_obs}): {len(near_death)}")
print(near_death.to_string(index=False))

# %% [markdown]
# ## 5. The smoking gun — did vehicle 1614 keep moving after we lost it?
#
# If the bus has GPS positions along the route AFTER the last emitted
# arrival, MARTA's tripupdates feed dropped the trip while the bus was
# still in service. If the GPS trail dies at roughly the same time, the
# bus actually pulled out of service.

# %%
gps = query(
    """
    SELECT v.snapshot_ts,
           v.veh_ts,
           v.trip_id,
           v.route_id,
           v.latitude,
           v.longitude,
           v.bearing,
           v.speed_mps
    FROM dbo.vehicle_position_snapshot v
    WHERE v.vehicle_id = ?
      AND v.snapshot_ts BETWEEN ? AND ?
    ORDER BY v.snapshot_ts
    """,
    (VEHICLE_ID, window_start.to_pydatetime(), window_end.to_pydatetime()),
)
print(f"{len(gps)} GPS pings for vehicle {VEHICLE_ID} in window")
if not gps.empty:
    print(f"  first ping            : {gps['snapshot_ts'].min()}")
    print(f"  last  ping            : {gps['snapshot_ts'].max()}")
    print(f"  trip_ids seen on bus  : {gps['trip_id'].dropna().unique().tolist()}")

    after_last = gps[gps["snapshot_ts"] > last_obs]
    on_trip_after = after_last[after_last["trip_id"] == TRIP_ID]
    print(f"\nPings AFTER last emitted arrival ({last_obs}):")
    print(f"  total pings after     : {len(after_last)}")
    print(f"  still tagged trip {TRIP_ID}: {len(on_trip_after)}")
    if len(after_last) > 0:
        # Look at the speed pattern — did the bus stop moving?
        moving_after = after_last[after_last["speed_mps"].fillna(0) > 1.0]
        print(f"  pings with speed > 1 m/s : {len(moving_after)}")

# %% [markdown]
# ## 6. Bonus — did this vehicle pick up a different trip immediately after?
#
# A common MARTA pattern: the bus finishes its run early, the driver swaps
# the headsign, and the bus is reassigned to the next trip on its block.
# That would explain a mid-route "end" cleanly.

# %%
if not gps.empty and (gps["snapshot_ts"] > last_obs).any():
    next_trips = (
        gps[gps["snapshot_ts"] > last_obs]
        .dropna(subset=["trip_id"])
        .groupby("trip_id")["snapshot_ts"]
        .agg(["min", "max", "count"])
        .reset_index()
        .sort_values("min")
    )
    print("Subsequent trip_ids on this vehicle (after last emitted arrival):")
    print(next_trips.to_string(index=False))
