// Tiny fetch wrapper around the C# /api/* endpoints. Returns parsed JSON
// or throws an Error with the server's message body.
//
// Dev: relative '/api' is proxied to localhost:7071 by Vite (vite.config.ts).
// Prod: SWA Free tier can't do a linked backend, so VITE_API_BASE is baked
// in at build time pointing at the Function App (e.g.
// https://busbunch-fn-xxxx.azurewebsites.net/api). The Function App has
// the SWA hostname allowlisted in CORS.

const BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/$/, '');

export async function apiGet<T>(path: string, params?: Record<string, string | number | undefined | null>): Promise<T> {
  const url = new URL(BASE + path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === '') continue;
      url.searchParams.set(k, String(v));
    }
  }
  const res = await fetch(url.toString(), { headers: { accept: 'application/json' } });
  if (!res.ok) {
    let body: unknown = null;
    try { body = await res.json(); } catch { /* ignore */ }
    const message = (body && typeof body === 'object' && 'error' in (body as Record<string, unknown>))
      ? String((body as Record<string, unknown>).error)
      : `HTTP ${res.status}`;
    throw new Error(message);
  }
  return res.json() as Promise<T>;
}

// ---- Response types (camelCase to match the API's JsonSerializerOptions) ----

export interface RouteRow {
  routeId: string;
  routeShortName: string | null;
  routeLongName: string | null;
}

export interface TrackedRouteRow extends RouteRow {
  addedAt: string;
}

export interface StopRow {
  stopSequence: number;
  stopId: string;
  stopName: string;
}

export interface VehicleRow {
  vehicleId: string;
  pingCount: number;
  lastSeenTs: string;
  latestRouteId: string | null;
  latestRouteShortName: string | null;
}

export interface MareyArrivalRow {
  observedArrivalTs: string;
  stopId: string;
  routeId: string;
  directionId: number | null;
  vehicleId: string | null;
  tripId: string;
}

export interface MareyResponse {
  spine: StopRow[];
  arrivals: MareyArrivalRow[];
}

export interface VehicleTrackRow {
  snapshotTs: string;
  vehicleId: string;
  routeId: string | null;
  routeShortName: string | null;
  directionId: number | null;
  tripId: string | null;
  latitude: number;
  longitude: number;
  bearing: number | null;
  speedMps: number | null;
}

export interface PredictionRow {
  snapshotTs: string;
  routeId: string;
  routeShortName: string | null;
  directionId: number | null;
  tripId: string;
  tripStartDate: string;
  stopSequence: number;
  stopId: string;
  stopName: string | null;
  vehicleId: string | null;
  predictedArrivalTs: string;
  observedArrivalTs: string | null;
  derivation: string | null;
  confidence: string | null;
  horizonSeconds: number | null;
  errorSeconds: number | null;
  arrivalResolution: 'matched' | 'suspect_cancelled' | 'unresolved';
  // Only present on the evolution endpoint.
  scheduledArrivalSeconds?: number | null;
}
