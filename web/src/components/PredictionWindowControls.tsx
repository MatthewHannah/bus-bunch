import { useState } from 'react';
import { apiGet, StopRow, TrackedRouteRow } from '../api';

export interface PredictionFormValue {
  stopId: string;
  routeShortName: string;
  directionId: number;
  startEt: string;
  endEt: string;
}

interface Props {
  value: PredictionFormValue;
  onChange: (v: PredictionFormValue) => void;
  trackedRoutes: TrackedRouteRow[];
  extra?: React.ReactNode;
  loading: boolean;
  onSubmit: (e: React.FormEvent) => void;
}

/**
 * Shared controls for the prediction error and prediction evolution pages.
 * Both pages take the same (stop, route filter, ET window) inputs; the
 * evolution endpoint just doesn't accept a horizon cap.
 *
 * "Browse stops on a route" loads the route's spine and offers it as a
 * picker, mirroring the convenience of editing STOP_ID in the python
 * scripts after picking a route.
 */
export default function PredictionWindowControls({
  value, onChange, trackedRoutes, extra, loading, onSubmit,
}: Props) {
  const [stopsForRoute, setStopsForRoute] = useState<StopRow[]>([]);
  const [stopBrowseRoute, setStopBrowseRoute] = useState<string>('');
  const [stopBrowseDir, setStopBrowseDir] = useState<number>(0);

  async function loadStops() {
    if (!stopBrowseRoute) return;
    const stops = await apiGet<StopRow[]>('/stops', {
      route_short_name: stopBrowseRoute,
      direction_id: stopBrowseDir,
    });
    setStopsForRoute(stops);
  }

  return (
    <form className="controls" onSubmit={onSubmit}>
      <label>
        Stop ID
        <input
          value={value.stopId}
          onChange={(e) => onChange({ ...value, stopId: e.target.value })}
          placeholder="e.g. 104076"
        />
      </label>
      <label>
        Route filter (optional)
        <select
          value={value.routeShortName}
          onChange={(e) => onChange({ ...value, routeShortName: e.target.value })}
        >
          <option value="">(all tracked routes)</option>
          {trackedRoutes.map((r) => (
            <option key={r.routeId} value={r.routeShortName ?? ''}>
              {r.routeShortName} — {r.routeLongName}
            </option>
          ))}
        </select>
      </label>
      <label>
        Direction (ignored if route blank)
        <select
          value={value.directionId}
          onChange={(e) => onChange({ ...value, directionId: Number(e.target.value) })}
        >
          <option value={0}>0</option>
          <option value={1}>1</option>
        </select>
      </label>
      <label>
        Start (ET)
        <input
          type="datetime-local"
          value={value.startEt}
          onChange={(e) => onChange({ ...value, startEt: e.target.value })}
        />
      </label>
      <label>
        End (ET)
        <input
          type="datetime-local"
          value={value.endEt}
          onChange={(e) => onChange({ ...value, endEt: e.target.value })}
        />
      </label>
      {extra}
      <div className="actions">
        <button type="submit" disabled={loading || !value.stopId}>
          {loading ? 'Loading…' : 'Render'}
        </button>
      </div>

      <fieldset
        style={{
          gridColumn: '1 / -1',
          border: '1px dashed var(--border)',
          borderRadius: 4,
          padding: '8px 12px',
        }}
      >
        <legend style={{ color: 'var(--fg-muted)', fontSize: 12 }}>
          Browse stops on a route (helper)
        </legend>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <input
            placeholder="route_short_name e.g. 21"
            value={stopBrowseRoute}
            onChange={(e) => setStopBrowseRoute(e.target.value)}
            style={{ padding: '4px 8px', border: '1px solid var(--border)', borderRadius: 4 }}
          />
          <select
            value={stopBrowseDir}
            onChange={(e) => setStopBrowseDir(Number(e.target.value))}
            style={{ padding: '4px 8px', border: '1px solid var(--border)', borderRadius: 4 }}
          >
            <option value={0}>dir 0</option>
            <option value={1}>dir 1</option>
          </select>
          <button type="button" onClick={loadStops} style={{ padding: '4px 12px' }}>
            Load stops
          </button>
          {stopsForRoute.length > 0 && (
            <select
              onChange={(e) => onChange({ ...value, stopId: e.target.value })}
              defaultValue=""
              style={{ padding: '4px 8px', border: '1px solid var(--border)', borderRadius: 4 }}
            >
              <option value="" disabled>
                pick a stop ({stopsForRoute.length})
              </option>
              {stopsForRoute.map((s) => (
                <option key={s.stopId} value={s.stopId}>
                  seq {s.stopSequence} · {s.stopId} · {s.stopName}
                </option>
              ))}
            </select>
          )}
        </div>
      </fieldset>
    </form>
  );
}
