import { useEffect, useMemo, useState } from 'react';
import Plot from '../components/Plot';
import PredictionWindowControls, {
  PredictionFormValue,
} from '../components/PredictionWindowControls';
import { apiGet, PredictionRow, TrackedRouteRow } from '../api';
import { defaultEtWindowInputs, etInputToUtcIso, utcToEtDate } from '../util/datetime';

// Stable color palette so each (route, trip, date) group keeps its color
// between the line and its X-on-zero arrival marker. Mirrors the
// per-trip coloring in scripts/viz/prediction_error.py.
const PALETTE = [
  '#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#FFA15A', '#19D3F3',
  '#FF6692', '#B6E880', '#FF97FF', '#FECB52',
];

export default function PredictionErrorPage() {
  const [tracked, setTracked] = useState<TrackedRouteRow[]>([]);
  const window0 = defaultEtWindowInputs(6);
  const [form, setForm] = useState<PredictionFormValue>({
    stopId: '',
    routeShortName: '',
    directionId: 0,
    startEt: window0.start,
    endEt: window0.end,
  });
  const [maxHorizonMin, setMaxHorizonMin] = useState<number | ''>(30);

  const [rows, setRows] = useState<PredictionRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resolutionCounts, setResolutionCounts] = useState<Record<string, number>>({});

  useEffect(() => {
    apiGet<TrackedRouteRow[]>('/tracked-routes')
      .then(setTracked)
      .catch((e: Error) => setError(e.message));
  }, []);

  async function load(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const raw = await apiGet<PredictionRow[]>('/prediction-error', {
        stop_id: form.stopId,
        route_short_name: form.routeShortName || undefined,
        direction_id: form.routeShortName ? form.directionId : undefined,
        start_utc: etInputToUtcIso(form.startEt),
        end_utc: etInputToUtcIso(form.endEt),
      });
      const counts: Record<string, number> = {};
      for (const r of raw) counts[r.arrivalResolution] = (counts[r.arrivalResolution] ?? 0) + 1;
      setResolutionCounts(counts);
      setRows(raw.filter((r) => r.arrivalResolution === 'matched'));
    } catch (e) {
      setError((e as Error).message);
      setRows([]);
      setResolutionCounts({});
    } finally {
      setLoading(false);
    }
  }

  const figure = useMemo(() => {
    if (rows.length === 0) return null;

    // Group by run_key = "rt {short} · {trip_id} · {trip_start_date}" when
    // multiple routes are returned, else just "trip · date". Matches the
    // run_key logic in the python script.
    const includeRoute = !form.routeShortName;
    type Pt = {
      x: Date; y: number; horizonMin: number; observedAtX: Date; vehicleId: string | null;
      predictedTs: string; observedTs: string;
    };
    const groups = new Map<string, Pt[]>();
    for (const r of rows) {
      if (r.observedArrivalTs == null || r.errorSeconds == null) continue;
      const snapshotX = utcToEtDate(r.snapshotTs);
      const observedX = utcToEtDate(r.observedArrivalTs);
      const horizonMin = (observedX.getTime() - snapshotX.getTime()) / 60_000;
      if (horizonMin < 0) continue;
      if (maxHorizonMin !== '' && horizonMin > maxHorizonMin) continue;
      const key = includeRoute
        ? `rt ${r.routeShortName} · ${r.tripId} · ${r.tripStartDate}`
        : `${r.tripId} · ${r.tripStartDate}`;
      const pts = groups.get(key) ?? [];
      pts.push({
        x: snapshotX,
        y: r.errorSeconds / 60,
        horizonMin,
        observedAtX: observedX,
        vehicleId: r.vehicleId,
        predictedTs: r.predictedArrivalTs,
        observedTs: r.observedArrivalTs,
      });
      groups.set(key, pts);
    }
    if (groups.size === 0) return null;

    for (const pts of groups.values()) pts.sort((a, b) => a.x.getTime() - b.x.getTime());

    const orderedKeys = [...groups.keys()];
    const colorFor = (k: string) => PALETTE[orderedKeys.indexOf(k) % PALETTE.length];

    const traces: unknown[] = orderedKeys.map((key) => {
      const pts = groups.get(key)!;
      return {
        type: 'scattergl' as const,
        mode: 'lines+markers' as const,
        name: key,
        x: pts.map((p) => p.x),
        y: pts.map((p) => p.y),
        line: { color: colorFor(key), width: 1.5 },
        marker: { color: colorFor(key), size: 4 },
        customdata: pts.map((p) => [p.vehicleId ?? '—', p.horizonMin.toFixed(1)]),
        hovertemplate:
          'snap %{x|%H:%M:%S}<br>err %{y:.2f} min<br>vehicle %{customdata[0]}<br>horizon %{customdata[1]} min<extra>' + key + '</extra>',
      };
    });

    // Find overall X range for the zero-reference line.
    let xLo = Number.POSITIVE_INFINITY, xHi = Number.NEGATIVE_INFINITY;
    for (const pts of groups.values()) {
      for (const p of pts) {
        xLo = Math.min(xLo, p.x.getTime());
        xHi = Math.max(xHi, p.x.getTime(), p.observedAtX.getTime());
      }
    }
    traces.push({
      type: 'scatter' as const,
      mode: 'lines' as const,
      name: 'perfect (y=0)',
      x: [new Date(xLo), new Date(xHi)],
      y: [0, 0],
      line: { color: 'lightgray', width: 1, dash: 'dot' },
      hoverinfo: 'skip' as const,
    });

    // Per-trip arrival marker on the zero line, color matched to the line.
    for (const key of orderedKeys) {
      const pts = groups.get(key)!;
      const observedAtX = pts[0].observedAtX;
      traces.push({
        type: 'scatter' as const,
        mode: 'markers' as const,
        name: `arrived · ${key}`,
        x: [observedAtX],
        y: [0],
        marker: { symbol: 'x', size: 10, color: colorFor(key) },
        hovertemplate: `arrived: %{x}<br>${key}<extra></extra>`,
        showlegend: false,
      });
    }

    return {
      data: traces,
      layout: {
        title: {
          text:
            `Prediction error — stop ${form.stopId} · ` +
            `${form.routeShortName || 'all tracked routes'} · ` +
            `${groups.size} trips`,
        },
        xaxis: { title: { text: 'Poll time (ET)' } },
        yaxis: { title: { text: 'Predicted − observed (minutes)' } },
        hovermode: 'closest' as const,
        margin: { l: 60, r: 20, t: 50, b: 50 },
        height: 700,
      },
      config: { responsive: true, displaylogo: false },
    };
  }, [rows, form.stopId, form.routeShortName, maxHorizonMin]);

  return (
    <>
      <h2 className="page-title">Prediction error</h2>
      <p className="page-blurb">
        For one stop + ET window, plots <code>predicted − observed</code>{' '}
        (minutes) for each trip. A healthy line tapers to zero as the bus
        arrives. Only trips with a resolved arrival are plotted; the
        resolution counts below show how many were dropped.
      </p>

      <PredictionWindowControls
        value={form}
        onChange={setForm}
        trackedRoutes={tracked}
        loading={loading}
        onSubmit={load}
        extra={
          <label>
            Max horizon (minutes)
            <input
              type="number"
              min={1}
              max={120}
              value={maxHorizonMin}
              onChange={(e) =>
                setMaxHorizonMin(e.target.value === '' ? '' : Number(e.target.value))
              }
              placeholder="(no cap)"
            />
          </label>
        }
      />

      {error && <div className="status error">{error}</div>}
      {!error && Object.keys(resolutionCounts).length > 0 && (
        <div className="status">
          Resolution mix:{' '}
          {Object.entries(resolutionCounts)
            .map(([k, n]) => `${k}=${n}`)
            .join(' · ')}
        </div>
      )}

      {figure && (
        <div className="plot-container">
          <Plot
            data={figure.data as never}
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
