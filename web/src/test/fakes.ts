// Test doubles for the two things the browser talks to: WS /live and fetch.
import { vi } from "vitest";
import type { LiveState } from "../types";

export class FakeSocket {
  static all: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) { FakeSocket.all.push(this); }
  close() { this.onclose?.(); }
  // test helpers
  open() { this.onopen?.(); }
  push(state: LiveState) { this.onmessage?.({ data: JSON.stringify(state) }); }
  static latest() { return FakeSocket.all[FakeSocket.all.length - 1]; }
}

export function installFakeSocket() {
  FakeSocket.all = [];
  vi.stubGlobal("WebSocket", FakeSocket);
}

export function liveState(calls: { call_id: string; persona?: string }[] = []): LiveState {
  return {
    tenant: "techbridge",
    pool: { capacity: 3, free: 3 - calls.length, busy: calls.length, free_agents: ["Alex", "Daniel"], busy_agents: ["Sarah"] },
    calls: calls.map((c) => ({ call_id: c.call_id, persona: c.persona ?? "Sarah", caller_id: "100", started_at: new Date().toISOString() })),
    counters: { uptime_s: 65, calls_total: 4, calls_rejected_total: 0, calls_failed_total: 1, transfers_total: 1, frames_dropped_total: 0, pacer_slips_total: 0 },
  };
}

/** A fetch that answers by URL prefix and records what was asked. */
export function installFakeFetch(routes: Record<string, unknown>) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    calls.push(url);
    const key = Object.keys(routes).sort((a, b) => b.length - a.length).find((k) => url.startsWith(k));
    const body = key ? routes[key] : { error: "not found" };
    return { ok: !!key, status: key ? 200 : 404, json: async () => body };
  }));
  return calls;
}
