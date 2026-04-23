# %% [markdown]
# # bus-bunch: bunching dashboards
#
# Three views into how clumpy MARTA buses are right now:
#
# 1. **Headway histogram** — distribution of gaps between buses at one stop.
#    Bimodal = bunching (spike at 0 + long tail).
# 2. **System bunching index over time** — fraction of headways <2 min,
#    bucketed by 15 minutes.
# 3. **Bubble map** — one dot per stop, colored by % bunched, sized by
#    arrivals/hr. Lights up the chronic corridors.
#
# Run cells top-to-bottom. Each chart is self-contained; skip any you don't want.

# %%
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.io as pio

from db import query

pio.renderers.default = "browser"  # change to "notebook" in Jupyter
pd.set_option("display.max_rows", 50)

# %% [markdown]
# ## Config — adjust these

# %%
LOOKBACK_HOURS = 24         # window for all queries below
FOCUS_STOP_ID  = "104076"   #
                            # To pick a new one, see the SELECT TOP query in
                            # tmp/scratch.sql or run:
                            #   SELECT TOP 10 a.stop_id, s.stop_name, COUNT(*) n
                            #   FROM dbo.stop_arrival_event a
                            #   JOIN dbo.stop s ON s.stop_id = a.stop_id
                            #   WHERE a.observed_arrival_ts > DATEADD(hour,-2,SYSUTCDATETIME())
                            #   GROUP BY a.stop_id, s.stop_name ORDER BY n DESC;
BUNCHED_SEC    = 300        # <5 min between buses = "bunched"
                            # Outside stations MARTA's tightest scheduled headway
                            # is ~15 min, so anything under 5 min on the trunk
                            # is almost certainly bunching.

# %% [markdown]
# ## 1. Headway histogram for one stop
#
# Healthy stop → tight bell centered on the scheduled headway.
# Bunched stop → bimodal: spike near 0 + long right tail.

# %%
hist_df = query(
    """
    SELECT h.headway_seconds / 60.0 AS headway_min,
           h.prev_route_id, h.route_id
    FROM dbo.vw_stop_trunk_headway h
    WHERE h.stop_id = ?
      AND h.observed_arrival_ts > DATEADD(hour, -?, SYSUTCDATETIME())
    """,
    (FOCUS_STOP_ID, LOOKBACK_HOURS),
)

stop_name = query(
    "SELECT stop_name FROM dbo.stop WHERE stop_id = ?", (FOCUS_STOP_ID,)
)["stop_name"].iloc[0]

fig = px.histogram(
    hist_df,
    x="headway_min",
    nbins=40,
    title=f"Trunk headway distribution — {stop_name} ({FOCUS_STOP_ID}) "
          f"— last {LOOKBACK_HOURS}h  (n={len(hist_df)})",
    labels={"headway_min": "Minutes between consecutive arrivals (any route)"},
)
fig.add_vline(x=BUNCHED_SEC / 60, line_dash="dot", line_color="red",
              annotation_text=f"bunched < {BUNCHED_SEC//60}m")
fig.update_layout(bargap=0.05)
fig.show()

# %% [markdown]
# ## 2. System-wide bunching index over time
#
# For each 15-minute bucket, what fraction of all headways across the system
# were bunched (< BUNCHED_SEC)? A "health of the system" line chart.

# %%
idx_df = query(
    """
    WITH b AS (
      SELECT
        DATEADD(MINUTE,
                (DATEDIFF(MINUTE, 0, observed_arrival_ts) / 15) * 15,
                0) AS bucket_utc,
        CASE WHEN headway_seconds < ? THEN 1 ELSE 0 END AS is_bunched
      FROM dbo.vw_stop_trunk_headway
      WHERE observed_arrival_ts > DATEADD(hour, -?, SYSUTCDATETIME())
    )
    SELECT bucket_utc,
           COUNT(*)                   AS n_headways,
           SUM(is_bunched)            AS n_bunched,
           1.0 * SUM(is_bunched) / COUNT(*) AS bunching_rate
    FROM b
    GROUP BY bucket_utc
    ORDER BY bucket_utc
    """,
    (BUNCHED_SEC, LOOKBACK_HOURS),
)
# UTC -> Eastern for readability
idx_df["bucket_et"] = (
    pd.to_datetime(idx_df["bucket_utc"])
      .dt.tz_localize("UTC")
      .dt.tz_convert("America/New_York")
)

fig = px.line(
    idx_df, x="bucket_et", y="bunching_rate",
    hover_data=["n_headways", "n_bunched"],
    title=f"System bunching index — last {LOOKBACK_HOURS}h "
          f"(% of headways under {BUNCHED_SEC//60}m, 15-min buckets)",
    labels={"bunching_rate": "Fraction bunched", "bucket_et": "Time (ET)"},
)
fig.update_yaxes(tickformat=".0%", rangemode="tozero")
fig.show()

# %% [markdown]
# ## 3. Geographic bubble map — chronic bunching corridors
#
# One dot per stop with ≥5 arrivals in the window. Color = % bunched.
# Size = arrivals/hour. You'll see Atlanta downtown trunk light up red.

# %%
geo_df = query(
    """
    SELECT
      h.stop_id,
      s.stop_name,
      s.stop_lat,
      s.stop_lon,
      COUNT(*)                                   AS n_headways,
      SUM(CASE WHEN headway_seconds < ? THEN 1 ELSE 0 END) AS n_bunched,
      AVG(headway_seconds) / 60.0                AS avg_headway_min
    FROM dbo.vw_stop_trunk_headway h
    JOIN dbo.stop s ON s.stop_id = h.stop_id
    WHERE h.observed_arrival_ts > DATEADD(hour, -?, SYSUTCDATETIME())
      AND s.stop_lat IS NOT NULL
      AND s.stop_lon IS NOT NULL
    GROUP BY h.stop_id, s.stop_name, s.stop_lat, s.stop_lon
    HAVING COUNT(*) >= 5
    """,
    (BUNCHED_SEC, LOOKBACK_HOURS),
)
geo_df["pct_bunched"] = geo_df["n_bunched"] / geo_df["n_headways"]
geo_df["arrivals_per_hr"] = geo_df["n_headways"] / LOOKBACK_HOURS

print(f"{len(geo_df)} stops plotted.")

# %% [markdown]
# ### Plotly map (interactive, opens in browser)

# %%
fig = px.scatter_map(
    geo_df,
    lat="stop_lat", lon="stop_lon",
    color="pct_bunched",
    size="arrivals_per_hr",
    hover_name="stop_name",
    hover_data={
        "stop_id": True,
        "n_headways": True,
        "n_bunched": True,
        "avg_headway_min": ":.1f",
        "pct_bunched": ":.0%",
        "stop_lat": False, "stop_lon": False,
    },
    color_continuous_scale="Reds",
    range_color=(0, 0.6),
    size_max=20,
    zoom=10,
    map_style="carto-positron",
    title=f"Bunching by stop — last {LOOKBACK_HOURS}h",
)
fig.show()

# %% [markdown]
# ### Folium alternative (saves to HTML file)

# %%
import folium
from branca.colormap import LinearColormap

cmap = LinearColormap(
    colors=["#2ecc71", "#f1c40f", "#e67e22", "#e74c3c", "#8e44ad"],
    vmin=0, vmax=0.6, caption=f"% bunched (<{BUNCHED_SEC//60}m headway)",
)

m = folium.Map(location=[33.755, -84.39], zoom_start=11, tiles="cartodbpositron")
for _, r in geo_df.iterrows():
    folium.CircleMarker(
        location=(r.stop_lat, r.stop_lon),
        radius=3 + min(r.arrivals_per_hr, 10),
        color=cmap(r.pct_bunched),
        fill=True, fill_opacity=0.75, weight=0.5,
        popup=folium.Popup(
            f"<b>{r.stop_name}</b> ({r.stop_id})<br>"
            f"{r.n_headways} arrivals, {r.n_bunched} bunched "
            f"({r.pct_bunched:.0%})<br>"
            f"avg headway {r.avg_headway_min:.1f} min",
            max_width=300),
    ).add_to(m)
cmap.add_to(m)

out = "bunching_map.html"
m.save(out)
print(f"Wrote {out} — open in a browser.")
