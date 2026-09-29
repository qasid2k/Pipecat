// Talking to the Python API. Plain fetch, relative URLs: in production the
// page is served by the same aiohttp server; in `npm run dev` Vite proxies these
// paths to a local bot (vite.config.ts).
import type { CallDetailBody, CallRow, Filters } from "./types";

export const HISTORY_LIMIT = 50;

/** The /history query for a set of filters. Empty fields are left out, so an
 *  empty filter asks for the plain newest-first list. */
export function historyQuery(filters: Filters, limit = HISTORY_LIMIT): string {
  const q = new URLSearchParams({ limit: String(limit) });
  for (const [key, value] of Object.entries(filters)) {
    const v = String(value).trim();
    if (v) q.set(key, v);
  }
  return q.toString();
}

/** Throws with the server's own error message (it sends `{"error": ...}` on a
 *  400/503), so the page can say WHY, not just "failed". */
async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
  return body as T;
}

export async function fetchHistory(filters: Filters): Promise<CallRow[]> {
  return (await getJson<{ calls: CallRow[] }>(`/history?${historyQuery(filters)}`)).calls;
}

export function fetchCallDetail(callId: string): Promise<CallDetailBody> {
  return getJson<CallDetailBody>(`/history/${encodeURIComponent(callId)}`);
}
