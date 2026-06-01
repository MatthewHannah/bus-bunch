import { useEffect, useMemo, useState } from 'react';
import Plot from '../components/Plot';
import PredictionWindowControls, {
  PredictionFormValue,
} from '../components/PredictionWindowControls';
import { apiGet, PredictionRow, TrackedRouteRow } from '../api';
import {
  defaultEtWindowInputs, etInputToUtcIso, scheduledToEtDate, utcToEtDate,
} from '../util/datetime';

const PALETTE = [
  '#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#FFA15A', '#19D3F3',
  '#FF6692', '#B6E880', '#FF97FF', '#FECB52',
];

export default function PredictionEvolutionPage() {
  const [tracked, setTracked] = useState<TrackedRouteRow[]>([]);
  const window0 = defaultEtWindowInputs(6);
  const [form, setForm] = useState<PredictionFormValue>({
    stopId: '',
    routeShortName: '',
    directionId: 0,
    startEt: window0.start,
    endEt: window0.end,
  });

  const [rows, setRows] = useState<PredictionRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
      const raw = await apiGet<PredictionRow[]>('/prediction-evolution', {
        stop_id: form.stopId,
        route_short_name: form.routeShortName || undefined,
        direction_id: form.routeShortName ? form.directionId : undefined,
        start_utc: etInputToUtcIso(form.startEt),
        end_utc: etInputToUtcIso(form.endEt),
      });
      setRows(raw);
    } catch (e) {
      setError((e as Error).message);
      setRows([]);
    } finally {
      setLoading(false);
    }
  }

  const figure = useMemo(() => {
    if (rows.length === 0) return null;

    const includeRoute = !form.routeShortName;
    type Pt = { snapshotX: Date; predictedX: Date; vehicleId: string | null; minutesOut: number };
    const groups = new Map<string, Pt[]>();
    const observedFor = new Map<string, Date>();
    const scheduledFor = new Map<string, Date>();

    for (const r of rows) {
      const snapshotX = utcToEtDate(r.snapshotTs);
      const predictedX = utcToEtDate(r.predictedArrivalTs);
      const minutesOut = (predictedX.getTime() - snapshotX.getTime()) / 60_000;
      const key = includeRoute
        ? `rt ${r.routeShortName} · ${r.tripId} · ${r.tripStartDate}`
        : `${r.tripId} · ${r.tripStartDate}`;
      const pts = groups.get(key) ?? [];
      pts.push({ snapshotX, predictedX, vehicleId: r.vehicleId, minutesOut });
      groups.set(key, pts);

      if (r.observedArrivalTs && !observedFor.has(key)) {
        observedFor.set(key, utcToEtDate(r.observedArrivalTs));
      }
      if (
        r.scheduledArrivalSeconds != null &&
        r.tripStartDate &&
        !scheduledFor.has(key)
      ) {
        scheduledFor.set(key, scheduledToEtDate(r.tripStartDate, r.scheduledArrivalSeconds));
      }
    }
    if (groups.size === 0) return null;

    for (const pts of groups.values()) {
      pts.sort((a, b) => a.snapshotX.getTime() - b.snapshotX.getTime());
    }

    const orderedKeys = [...groups.keys()];
    const colorFor = (k: string) => PALETTE[orderedKeys.indexOf(k) % PALETTE.length];

    const traces: unknown[] = orderedKeys.map((key) => {
      const pts = groups.get(key)!;
      return {
        type: 'scattergl' as const,
        mode: 'lines+markers' as const,
        name: key,
        x: pts.map((p) => p.snapshotX),
        y: pts.map((p) => p.predictedX),
        line: { color: colorFor(key), width: 1.5 },
        marker: { color: colorFor(key), size: 4 },
        customdata: pts.map((p) => [p.vehicleId ?? '—', p.minutesOut.toFixed(1)]),
        hovertemplate:
          'snap %{x|%H:%M:%S}<br>predicted %{y|%H:%M:%S}<br>vehicle %{customdata[0]}<br>%{customdata[1]} min out<extra>' + key + '</extra>',
      };
    });

    // y = x "now" diagonal: anchored to the actual data range.
    let xLo = Number.POSITIVE_INFINITY, xHi = Number.NEGATIVE_INFINITY;
    for (const pts of groups.values()) {
      for (const p of pts) {
        xLo = Math.min(xLo, p.snapshotX.getTime(), p.predictedX.getTime());
        xHi = Math.max(xHi, p.snapshotX.getTime(), p.predictedX.getTime());
      }
    }
    traces.push({
      type: 'scatter' as const,
      mode: 'lines' as const,
      name: 'now (y=x)',
      x: [new Date(xLo), new Date(xHi)],
      y: [new Date(xLo), new Date(xHi)],
      line: { color: 'lightgray', width: 1 },
      hoverinfo: 'skip' as const,
    });

    // Per-trip observed (dashed) and scheduled (dotted) horizontals,
    // anchored to that trip's actual X range.
    for (const key of orderedKeys) {
      const pts = groups.get(key)!;
      const x0 = pts[0].snapshotX;
      const x1 = pts[pts.length - 1].snapshotX;
      const observed = observedFor.get(key);
      if (observed) {
        traces.push({
          type: 'scatter' as const,
          mode: 'lines' as const,
          name: `observed · ${key}`,
          x: [x0, x1],
          y: [observed, observed],
          line: { color: colorFor(key), width: 1, dash: 'dash' },
          hovertemplate: `observed arrival: %{y}<br>${key}<extra></extra>`,
          showlegend: false,
        });
      }
      const scheduled = scheduledFor.get(key);
      if (scheduled) {
        traces.push({
          type: 'scatter' as const,
          mode: 'lines' as const,
          name: `scheduled · ${key}`,
          x: [x0, x1],
          y: [scheduled, scheduled],
          line: { color: colorFor(key), width: 1, dash: 'dot' },
          hovertemplate: `scheduled arrival: %{y}<br>${key}<extra></extra>`,
          showlegend: false,
        });
      }
    }

    return {
      data: traces,
      layout: {
        title: {
          text:
            `Prediction evolution — stop ${form.stopId} · ` +
            `${form.routeShortName || 'all tracked routes'} · ` +
            `${groups.size} trips`,
        },
        xaxis: { title: { text: 'Poll time (ET)' } },
        yaxis: { title: { text: 'Predicted arrival (ET)' }, type: 'date' as const },
        hovermode: 'closest' as const,
        margin: { l: 80, r: 20, t: 50, b: 50 },
        height: 700,
      },
      config: { responsive: true, displaylogo: false },
    };
  }, [rows, form.stopId, form.routeShortName]);

  return (
    <>
      <h2 className="page-title">Prediction evolution</h2>
      <p className="page-blurb">
        How each trip's ETA at this stop changed over successive polls. Per
        trip, the same color is used for the prediction line, the dashed
        observed-arrival horizontal, and the dotted scheduled-arrival
        horizontal — convergence of a line onto its own dashed line means
        MARTA's last ETA matched reality.
      </p>

      <PredictionWindowControls
        value={form}
        onChange={setForm}
        trackedRoutes={tracked}
        loading={loading}
        onSubmit={load}
      />

      {error && <div className="status error">{error}</div>}
      {!error && rows.length > 0 && (
        <div className="status">
          {rows.length} prediction rows
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
