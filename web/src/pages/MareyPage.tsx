import { useEffect, useMemo, useState } from 'react';
import Plot from '../components/Plot';
import { apiGet, MareyResponse, RouteRow } from '../api';
import { utcToEtDate, utcToEtLabel } from '../util/datetime';

export default function MareyPage() {
  const [routes, setRoutes] = useState<RouteRow[]>([]);
  const [routeShortName, setRouteShortName] = useState<string>('');
  const [directionId, setDirectionId] = useState<number>(0);
  const [lookbackHours, setLookbackHours] = useState<number>(24);

  const [data, setData] = useState<MareyResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<RouteRow[]>('/routes').then((rs) => {
      setRoutes(rs);
      // Default to the first route that has a recognizable short_name.
      const first = rs.find((r) => r.routeShortName);
      if (first?.routeShortName) setRouteShortName(first.routeShortName);
    }).catch((e: Error) => setError(e.message));
  }, []);

  async function load(e?: React.FormEvent) {
    e?.preventDefault();
    if (!routeShortName) return;
    setLoading(true);
    setError(null);
    try {
      const resp = await apiGet<MareyResponse>('/marey', {
        route_short_name: routeShortName,
        direction_id: directionId,
        lookback_hours: lookbackHours,
      });
      setData(resp);
    } catch (e) {
      setError((e as Error).message);
      setData(null);
    } finally {
      setLoading(false);
    }
  }

  const figure = useMemo(() => {
    if (!data || data.spine.length === 0) return null;

    const seqByStop = new Map(data.spine.map((s) => [s.stopId, s.stopSequence] as const));
    const nameByStop = new Map(data.spine.map((s) => [s.stopId, s.stopName] as const));

    // Group arrivals by (vehicle_id, trip_id) — the "run" — and sort each
    // group by time so the polyline traces are well-formed. Mirrors the
    // run_key construction in scripts/viz/marey.py.
    type Pt = { ts: string; seq: number; stopId: string };
    const runs = new Map<string, Pt[]>();
    for (const a of data.arrivals) {
      const seq = seqByStop.get(a.stopId);
      if (seq === undefined) continue;
      const key = `${a.vehicleId ?? '?'} · ${a.tripId}`;
      const pts = runs.get(key) ?? [];
      pts.push({ ts: a.observedArrivalTs, seq, stopId: a.stopId });
      runs.set(key, pts);
    }
    for (const pts of runs.values()) {
      pts.sort((a, b) => a.ts.localeCompare(b.ts));
    }

    const traces = [...runs.entries()].map(([key, pts]) => ({
      type: 'scattergl' as const,
      mode: 'lines+markers' as const,
      name: key,
      x: pts.map((p) => utcToEtDate(p.ts)),
      y: pts.map((p) => p.seq),
      marker: { size: 5 },
      line: { width: 1.5 },
      hovertemplate:
        '%{x|%a %H:%M:%S}<br>seq %{y} · %{customdata}<extra>' + key + '</extra>',
      customdata: pts.map((p) => nameByStop.get(p.stopId) ?? p.stopId),
      showlegend: false,
    }));

    return {
      data: traces,
      layout: {
        title: {
          text:
            `Marey — route ${routeShortName} dir ${directionId} (last ${lookbackHours}h, ${runs.size} runs)`,
        },
        xaxis: { title: { text: 'Time (ET)' } },
        yaxis: {
          title: { text: 'Stop sequence' },
          autorange: 'reversed' as const,
        },
        hovermode: 'closest' as const,
        margin: { l: 60, r: 20, t: 50, b: 50 },
        height: 700,
      },
      config: { responsive: true, displaylogo: false },
    };
  }, [data, routeShortName, directionId, lookbackHours]);

  return (
    <>
      <h2 className="page-title">Marey diagram</h2>
      <p className="page-blurb">
        X = time (ET); Y = stop sequence along the route spine. Each polyline
        is one bus run. Lines that converge horizontally = bunching forming
        in real time; empty horizontal bands = service gaps.
      </p>

      <form className="controls" onSubmit={load}>
        <label>
          Route
          <select value={routeShortName} onChange={(e) => setRouteShortName(e.target.value)}>
            {routes.map((r) => (
              <option key={r.routeId} value={r.routeShortName ?? ''}>
                {r.routeShortName} — {r.routeLongName}
              </option>
            ))}
          </select>
        </label>
        <label>
          Direction
          <select value={directionId} onChange={(e) => setDirectionId(Number(e.target.value))}>
            <option value={0}>0</option>
            <option value={1}>1</option>
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
          <button type="submit" disabled={loading || !routeShortName}>
            {loading ? 'Loading…' : 'Render'}
          </button>
        </div>
      </form>

      {error && <div className="status error">{error}</div>}
      {data && !error && (
        <div className="status">
          {data.spine.length} stops on spine · {data.arrivals.length} arrivals · latest{' '}
          {data.arrivals.length > 0
            ? utcToEtLabel(data.arrivals[data.arrivals.length - 1].observedArrivalTs)
            : '—'}
        </div>
      )}

      {figure && (
        <div className="plot-container">
          <Plot
            data={figure.data}
            layout={figure.layout}
            config={figure.config}
            useResizeHandler
            style={{ width: '100%', height: '100%' }}
          />
        </div>
      )}
    </>
  );
}
