# %% [markdown]
# # Single-vehicle trail
#
# Map the path of one bus (by `vehicle_id`) over the last N hours of
# `vehicle_position_snapshot` rows, with a time slider that highlights the
# vehicle's exact position at the chosen moment.
#
# - Faint polyline = the entire trail in the window.
# - Big highlighted dot = position at the time selected by the slider.
# - Slider step = one per snapshot (or one per `SLIDER_STEP_MIN` minutes if
#   you set that > 0 and want a coarser scrubber).
# - **⏮ ◀ ▶ ⏭ buttons** step through snapshots one timestamp at a time, in
#   addition to dragging the slider.
#
# Run as a script (opens in browser) or step through the cells in VS Code.

# %%
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

from db import query

pio.renderers.default = "browser"

# %% [markdown]
# ## Config

# %%
VEHICLE_ID      = "1544"   # MARTA vehicle ID. To find busy ones, run:
                           #   SELECT TOP 20 vehicle_id, COUNT(*) n
                           #   FROM dbo.vehicle_position_snapshot
                           #   WHERE snapshot_ts > DATEADD(hour,-24,SYSUTCDATETIME())
                           #   GROUP BY vehicle_id ORDER BY n DESC;
LOOKBACK_HOURS  = 24
SLIDER_STEP_MIN = 0        # 0 = one slider tick per snapshot (recommended,
                           #     so prev/next buttons step one ping at a time);
                           # >0 = bucket the slider to that many minutes for a
                           #     coarser scrubber. Step buttons still walk
                           #     every snapshot regardless.

# %% [markdown]
# ## 1. Pull the trail

# %%
trail = query(
    """
    SELECT
        v.snapshot_ts,
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
      AND v.snapshot_ts > DATEADD(hour, -?, SYSUTCDATETIME())
      AND v.latitude  IS NOT NULL
      AND v.longitude IS NOT NULL
    ORDER BY v.snapshot_ts
    """,
    (VEHICLE_ID, LOOKBACK_HOURS),
)

if trail.empty:
    raise SystemExit(
        f"No positions for vehicle_id={VEHICLE_ID!r} in the last "
        f"{LOOKBACK_HOURS}h. Try a different ID — see the SELECT TOP query "
        "in the config cell above."
    )

trail["snapshot_et"] = (
    pd.to_datetime(trail["snapshot_ts"])
      .dt.tz_localize("UTC")
      .dt.tz_convert("America/New_York")
)
trail["latitude"]  = trail["latitude"].astype(float)
trail["longitude"] = trail["longitude"].astype(float)
print(f"{len(trail)} positions for vehicle {VEHICLE_ID} over {LOOKBACK_HOURS}h")

# %% [markdown]
# ## 2. Decide which snapshots become slider ticks
#
# Each snapshot becomes a Plotly *frame* (so the prev/next buttons can step
# one ping at a time). The *slider* either has one tick per frame
# (`SLIDER_STEP_MIN = 0`) or is bucketed for a coarser scrubber.

# %%
frames_df = trail.sort_values("snapshot_et").reset_index(drop=True)

if SLIDER_STEP_MIN and SLIDER_STEP_MIN > 0:
    bucket = frames_df["snapshot_et"].dt.floor(f"{SLIDER_STEP_MIN}min")
    slider_df = (
        frames_df.assign(_bucket=bucket, _frame_idx=frames_df.index)
                 .drop_duplicates("_bucket", keep="last")
                 .reset_index(drop=True)
    )
else:
    slider_df = frames_df.assign(_frame_idx=frames_df.index)

print(f"{len(frames_df)} frames · {len(slider_df)} slider ticks")

# %% [markdown]
# ## 3. Build the figure
#
# Two map traces:
#   0. faint trail (line + small dots), always visible
#   1. highlighted "current position" marker, swapped per frame via the slider

# %%
center_lat = float(trail["latitude"].mean())
center_lon = float(trail["longitude"].mean())

trail_trace = go.Scattermap(
    lat=trail["latitude"],
    lon=trail["longitude"],
    mode="lines+markers",
    line=dict(color="#3498db", width=2),
    marker=dict(size=4, color="#3498db", opacity=0.5),
    name="trail",
    hoverinfo="skip",
)

def _hover(row: pd.Series) -> str:
    rt = row["route_short_name"] or row["route_id"] or "—"
    spd = "" if pd.isna(row["speed_mps"]) else f"{row['speed_mps'] * 2.237:.1f} mph"
    return (
        f"<b>{row['snapshot_et']:%a %H:%M:%S}</b><br>"
        f"route {rt} · dir {row['direction_id']}<br>"
        f"trip {row['trip_id'] or '—'}<br>"
        f"{row['latitude']:.5f}, {row['longitude']:.5f}<br>"
        f"{spd}".rstrip()
    )

first = frames_df.iloc[0]
current_trace = go.Scattermap(
    lat=[first["latitude"]],
    lon=[first["longitude"]],
    mode="markers",
    marker=dict(size=18, color="#e74c3c"),
    name="position",
    hovertext=[_hover(first)],
    hoverinfo="text",
)

frames = [
    go.Frame(
        name=str(i),
        data=[
            go.Scattermap(
                lat=[r["latitude"]],
                lon=[r["longitude"]],
                mode="markers",
                marker=dict(size=18, color="#e74c3c"),
                hovertext=[_hover(r)],
                hoverinfo="text",
            )
        ],
        traces=[1],  # only update the "current position" trace
    )
    for i, r in frames_df.iterrows()
]

slider_steps = [
    {
        "method": "animate",
        "label": r["snapshot_et"].strftime("%H:%M:%S"),
        "args": [
            [str(int(r["_frame_idx"]))],
            {
                "mode": "immediate",
                "frame": {"duration": 0, "redraw": True},
                "transition": {"duration": 0},
            },
        ],
    }
    for _, r in slider_df.iterrows()
]

route_label = (
    trail["route_short_name"].dropna().iloc[0]
    if trail["route_short_name"].notna().any()
    else trail["route_id"].dropna().iloc[0] if trail["route_id"].notna().any()
    else "?"
)

fig = go.Figure(
    data=[trail_trace, current_trace],
    frames=frames,
    layout=go.Layout(
        title=(
            f"Vehicle {VEHICLE_ID} — last {LOOKBACK_HOURS}h "
            f"(latest route: {route_label}, {len(trail)} pings)"
        ),
        map=dict(
            style="carto-positron",
            center=dict(lat=center_lat, lon=center_lon),
            zoom=11,
        ),
        margin=dict(l=0, r=0, t=40, b=0),
        sliders=[
            {
                "active": 0,
                "currentvalue": {"prefix": "Time (ET): "},
                "pad": {"t": 40, "b": 10},
                "steps": slider_steps,
            }
        ],
        updatemenus=[
            {
                "type": "buttons",
                "showactive": False,
                "y": 0,
                "x": 0,
                "xanchor": "left",
                "yanchor": "top",
                "pad": {"t": 40, "r": 10},
                "buttons": [
                    {
                        "label": "▶ play",
                        "method": "animate",
                        "args": [
                            None,
                            {
                                "frame": {"duration": 200, "redraw": True},
                                "fromcurrent": True,
                                "transition": {"duration": 0},
                            },
                        ],
                    },
                    {
                        "label": "❚❚ pause",
                        "method": "animate",
                        "args": [
                            [None],
                            {
                                "frame": {"duration": 0, "redraw": False},
                                "mode": "immediate",
                                "transition": {"duration": 0},
                            },
                        ],
                    },
                ],
            }
        ],
    ),
)

# %% [markdown]
# ## 4. Inject prev/next "step one snapshot" buttons
#
# Plotly's built-in updatemenu buttons can only animate to absolute frame
# names — they can't say "go to the next frame". So we render to HTML and
# inject a tiny JS snippet that:
#
# * adds ⏮ ◀ ▶ ⏭ buttons in the top-right of the plot
# * tracks the current frame index (initially 0, updated whenever the user
#   moves the slider, plays, or clicks a step button)
# * on click, calls `Plotly.animate(gd, [frameName], …)` to jump one frame
#
# Each Plotly frame corresponds to one snapshot, so a single click = exactly
# one timestamp forward/back.

# %%
import os
import tempfile
import webbrowser

_STEP_JS = r"""
var gd = document.getElementById('{plot_id}');
if (gd) {
  var frameNames = (gd._transitionData && gd._transitionData._frames || [])
      .map(function (f) { return f && f.name; })
      .filter(function (n) { return n != null; });
  if (frameNames.length) {
    var current = 0;
    function jump(idx) {
      if (idx < 0) idx = 0;
      if (idx >= frameNames.length) idx = frameNames.length - 1;
      current = idx;
      Plotly.animate(gd, [frameNames[idx]], {
        mode: 'immediate',
        frame: {duration: 0, redraw: true},
        transition: {duration: 0}
      });
    }
    // keep `current` in sync with whatever Plotly is showing
    gd.on('plotly_animatingframe', function (e) {
      if (e && e.name != null) {
        var i = frameNames.indexOf(String(e.name));
        if (i >= 0) current = i;
      }
    });

    var bar = document.createElement('div');
    bar.style.cssText =
      'position:absolute;top:8px;right:12px;z-index:1000;display:flex;' +
      'gap:4px;font-family:sans-serif;font-size:12px;';
    function mkBtn(label, title, onClick) {
      var b = document.createElement('button');
      b.textContent = label;
      b.title = title;
      b.style.cssText =
        'padding:4px 9px;cursor:pointer;border:1px solid #888;' +
        'background:#fff;border-radius:4px;line-height:1;';
      b.onclick = onClick;
      return b;
    }
    bar.appendChild(mkBtn('\u23EE', 'first snapshot',
                          function () { jump(0); }));
    bar.appendChild(mkBtn('\u25C0', 'previous snapshot',
                          function () { jump(current - 1); }));
    bar.appendChild(mkBtn('\u25B6', 'next snapshot',
                          function () { jump(current + 1); }));
    bar.appendChild(mkBtn('\u23ED', 'last snapshot',
                          function () { jump(frameNames.length - 1); }));

    var host = gd.querySelector('.plot-container') || gd;
    if (getComputedStyle(host).position === 'static') {
      host.style.position = 'relative';
    }
    host.appendChild(bar);

    // arrow keys also step
    gd.tabIndex = 0;
    gd.addEventListener('keydown', function (ev) {
      if (ev.key === 'ArrowRight') { jump(current + 1); ev.preventDefault(); }
      else if (ev.key === 'ArrowLeft') { jump(current - 1); ev.preventDefault(); }
    });
  }
}
""".strip()

_out_path = os.path.join(tempfile.gettempdir(), f"vehicle_track_{VEHICLE_ID}.html")
fig.write_html(_out_path, post_script=_STEP_JS, include_plotlyjs="cdn")
webbrowser.open("file://" + _out_path)
print(f"Wrote {_out_path}")

# %% [markdown]
# ### Reading the chart
#
# * The blue polyline is everywhere the bus has been in the window.
# * Drag the slider (or hit ▶ play) to scrub through time; the red dot snaps
#   to where the bus actually was at that moment.
# * Use the **⏮ ◀ ▶ ⏭** buttons (top-right) — or the keyboard arrow keys
#   when the chart has focus — to step one snapshot at a time.
# * Hover the red dot for route/trip/speed at that moment.
# * Tight clusters of blue dots = the bus sitting still (layover, traffic,
#   bunched at a stop). Long straight runs with sparse dots = highway / express.
