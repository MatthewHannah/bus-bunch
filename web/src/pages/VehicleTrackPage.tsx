import { useEffect, useMemo, useState } from 'react';
import Plot from '../components/Plot';
import { apiGet, VehicleRow, VehicleTrackRow } from '../api';
import { utcToEtLabel } from '../util/datetime';

export default function VehicleTrackPage() {
  const [vehicles, setVehicles] = useState<VehicleRow[]>([]);
  const [vehicleId, setVehicleId] = useState<string>('');
  const [lookbackHours, setLookbackHours] = useState<number>(24);

  const [trail, setTrail] = useState<VehicleTrackRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<VehicleRow[]>('/vehicles', { lookback_hours: lookbackHours })
      .then((vs) => {
        setVehicles(vs);
        if (vs.length > 0 && !vehicleId) setVehicleId(vs[0].vehicleId);
      })
      .catch((e: Error) => setError(e.message));
    // Only refresh the picker when the lookback changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lookbackHours]);

  async function load(e?: React.FormEvent) {
    e?.preventDefault();
    if (!vehicleId) return;
    setLoading(true);
    setError(null);
    try {
      const resp = await apiGet<VehicleTrackRow[]>('/vehicle-track', {
        vehicle_id: vehicleId,
        lookback_hours: lookbackHours,
      });
      setTrail(resp);
    } catch (e) {
      setError((e as Error).message);
      setTrail([]);
    } finally {
      setLoading(false);
    }
  }

  const figure = useMemo(() => {
    if (trail.length === 0) return null;

    const lats = trail.map((r) => r.latitude);
    const lons = trail.map((r) => r.longitude);
    const centerLat = lats.reduce((a, b) => a + b, 0) / lats.length;
    const centerLon = lons.reduce((a, b) => a + b, 0) / lons.length;

    const hoverText = trail.map((r) => {
      const rt = r.routeShortName ?? r.routeId ?? '—';
      const spd = r.speedMps == null ? '' : `${(r.speedMps * 2.237).toFixed(1)} mph`;
      return [
        `<b>${utcToEtLabel(r.snapshotTs, 'ddd HH:mm:ss')}</b>`,
        `route ${rt} · dir ${r.directionId ?? '—'}`,
        `trip ${r.tripId ?? '—'}`,
        `${r.latitude.toFixed(5)}, ${r.longitude.toFixed(5)}`,
        spd,
      ].filter(Boolean).join('<br>');
    });

    // Trace 0: the faint full trail (always visible).
    // Trace 1: the highlighted "current" marker — swapped per frame by the
    // slider, exactly like scripts/viz/vehicle_track.py.
    const trailTrace = {
      type: 'scattermap' as const,
      mode: 'lines+markers' as const,
      lat: lats,
      lon: lons,
      line: { color: '#3498db', width: 2 },
      marker: { size: 4, color: '#3498db' },
      name: 'trail',
      hoverinfo: 'skip' as const,
    };

    const currentTrace = {
      type: 'scattermap' as const,
      mode: 'markers' as const,
      lat: [trail[0].latitude],
      lon: [trail[0].longitude],
      marker: { size: 16, color: '#e74c3c' },
      hovertext: [hoverText[0]],
      hoverinfo: 'text' as const,
      name: 'position',
    };

    const frames = trail.map((r, i) => ({
      name: String(i),
      data: [
        {
          type: 'scattermap' as const,
          mode: 'markers' as const,
          lat: [r.latitude],
          lon: [r.longitude],
          marker: { size: 16, color: '#e74c3c' },
          hovertext: [hoverText[i]],
          hoverinfo: 'text' as const,
        },
      ],
      traces: [1],
    }));

    const steps = trail.map((r, i) => ({
      method: 'animate' as const,
      label: utcToEtLabel(r.snapshotTs, 'HH:mm:ss'),
      args: [
        [String(i)],
        {
          mode: 'immediate',
          frame: { duration: 0, redraw: true },
          transition: { duration: 0 },
        },
      ],
    }));

    return {
      data: [trailTrace, currentTrace],
      frames,
      layout: {
        title: {
          text:
            `Vehicle ${vehicleId} — last ${lookbackHours}h (${trail.length} pings)`,
        },
        map: {
          style: 'carto-positron',
          center: { lat: centerLat, lon: centerLon },
          zoom: 11,
        },
        margin: { l: 0, r: 0, t: 40, b: 0 },
        height: 700,
        sliders: [
          {
            active: 0,
            currentvalue: { prefix: 'Time (ET): ' },
            pad: { t: 40, b: 10 },
            steps,
          },
        ],
        updatemenus: [
          {
            type: 'buttons',
            showactive: false,
            y: 0,
            x: 0,
            xanchor: 'left',
            yanchor: 'top',
            pad: { t: 40, r: 10 },
            buttons: [
              {
                label: '▶ play',
                method: 'animate',
                args: [
                  null,
                  {
                    frame: { duration: 200, redraw: true },
                    fromcurrent: true,
                    transition: { duration: 0 },
                  },
                ],
              },
              {
                label: '❚❚ pause',
                method: 'animate',
                args: [
                  [null],
                  {
                    frame: { duration: 0, redraw: false },
                    mode: 'immediate',
                    transition: { duration: 0 },
                  },
                ],
              },
            ],
          },
        ],
      },
      config: { responsive: true, displaylogo: false },
    };
  }, [trail, vehicleId, lookbackHours]);

  return (
    <>
      <h2 className="page-title">Vehicle track</h2>
      <p className="page-blurb">
        Map trail for one MARTA <code>vehicle_id</code> over the lookback
        window. Drag the slider or hit ▶ play to scrub through the snapshots
        — the red dot snaps to the bus's position at the chosen time.
      </p>

      <form className="controls" onSubmit={load}>
        <label>
          Vehicle
          <select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
            {vehicles.map((v) => (
              <option key={v.vehicleId} value={v.vehicleId}>
                {v.vehicleId} ({v.pingCount} pings
                {v.latestRouteShortName ? ` · rt ${v.latestRouteShortName}` : ''})
              </option>
            ))}
          </select>
        </label>
        <label>
          Lookback (hours)
          <input
            type="number"
            min={1}
            max={168}
            value={lookbackHours}
            onChange={(e) => setLookbackHours(Number(e.target.value))}
          />
        </label>
        <div className="actions">
          <button type="submit" disabled={loading || !vehicleId}>
            {loading ? 'Loading…' : 'Render'}
          </button>
        </div>
      </form>

      {error && <div className="status error">{error}</div>}
      {!error && trail.length > 0 && (
        <div className="status">
          {trail.length} pings from{' '}
          {utcToEtLabel(trail[0].snapshotTs)} to{' '}
          {utcToEtLabel(trail[trail.length - 1].snapshotTs)}
        </div>
      )}

      {figure && (
        <div className="plot-container">
          <Plot
            data={figure.data as never}
            frames={figure.frames as never}
            layout={figure.layout as never}
            config={figure.config}
            useResizeHandler
            style={{ width: '100%', height: '100%' }}
          />
        </div>
      )}
    </>
  );
}
