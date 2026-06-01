// Eastern-time / UTC helpers used across the prediction pages. The C# API
// emits all timestamps as ISO 8601 UTC with a trailing 'Z'; the frontend is
// responsible for ET display and for converting user-entered ET window
// bounds back to UTC for the API.

import dayjs from 'dayjs';
import utc from 'dayjs/plugin/utc';
import timezone from 'dayjs/plugin/timezone';

dayjs.extend(utc);
dayjs.extend(timezone);

export const ET = 'America/New_York';

/** Format a UTC ISO string as wall-clock Eastern. */
export function utcToEtLabel(iso: string, fmt = 'YYYY-MM-DD HH:mm:ss'): string {
  return dayjs.utc(iso).tz(ET).format(fmt);
}

/** Take an ET "YYYY-MM-DDTHH:mm" string and convert to a UTC ISO string. */
export function etInputToUtcIso(etLocal: string): string {
  // dayjs.tz parses an unzoned wall-clock string in the given tz.
  return dayjs.tz(etLocal, ET).utc().format('YYYY-MM-DDTHH:mm:ss[Z]');
}

/** Convert an ISO UTC timestamp to a JS Date in the ET wall clock. */
export function utcToEtDate(iso: string): Date {
  // For plotly we need a JS Date whose UTC interpretation matches the ET
  // wall clock of the input — only then will Plotly's axis labels show ET
  // values directly. (Plotly does not have built-in tz support.)
  const m = dayjs.utc(iso).tz(ET);
  return new Date(Date.UTC(m.year(), m.month(), m.date(), m.hour(), m.minute(), m.second()));
}

/** Default ET window (last N hours, rounded down to the minute). */
export function defaultEtWindowInputs(hoursBack: number): { start: string; end: string } {
  const end = dayjs.tz(undefined, ET).startOf('minute');
  const start = end.subtract(hoursBack, 'hour');
  const fmt = 'YYYY-MM-DDTHH:mm';
  return { start: start.format(fmt), end: end.format(fmt) };
}

/**
 * Static schedule arrival_time is "seconds since service-day local midnight"
 * and may exceed 86400 for after-midnight trips. Combine with trip_start_date
 * (YYYYMMDD) in ET to get a real wall-clock instant.
 */
export function scheduledToEtDate(tripStartDate: string, scheduledSeconds: number): Date {
  const m = dayjs.tz(
    `${tripStartDate.slice(0, 4)}-${tripStartDate.slice(4, 6)}-${tripStartDate.slice(6, 8)}T00:00:00`,
    ET,
  ).add(scheduledSeconds, 'second');
  return new Date(Date.UTC(m.year(), m.month(), m.date(), m.hour(), m.minute(), m.second()));
}
