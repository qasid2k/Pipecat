import { AlertTriangle, CheckCircle2, WifiOff } from "lucide-react";
import { Card, PageHeader, StatusPill } from "../components/Shell";
import { hhmmss, useNow } from "../format";
import type { LiveCall, LiveState } from "../types";
import type { LiveStatus } from "../useLive";

/** Every agent in the roster, each with the call it is on (if any). Stable
 *  alphabetical order, so a card doesn't jump around when its status changes. */
export function agentsWithCalls(state: LiveState): { name: string; call: LiveCall | null }[] {
  const byAgent = new Map(state.calls.map((c) => [c.persona, c]));
  return [...state.pool.free_agents, ...state.pool.busy_agents]
    .sort((a, b) => a.localeCompare(b))
    .map((name) => ({ name, call: byAgent.get(name) ?? null }));
}

function Kpi({ label, value, sub, warn = false, children }: {
  label: string; value: string | number; sub?: string; warn?: boolean; children?: React.ReactNode;
}) {
  return (
    <div
      data-warn={warn}
      className={`rounded-xl border p-4 ${warn ? "border-bad/40 bg-bad-soft" : "border-border bg-surface"}`}
    >
      <div className="text-xs font-medium text-muted">{label}</div>
      <div className={`mt-1 text-3xl font-semibold tabular-nums ${warn ? "text-bad" : ""}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
      {children}
    </div>
  );
}

function AgentCard({ name, call, now }: { name: string; call: LiveCall | null; now: number }) {
  const busy = call !== null;
  const secs = call ? (now - new Date(call.started_at).getTime()) / 1000 : 0;
  return (
    <div
      role="group"
      aria-label={busy ? `${name}, on a call for ${hhmmss(secs)}` : `${name}, free`}
      tabIndex={0}
      className={`rounded-xl border p-4 transition-colors ${
        busy ? "border-busy/50 bg-busy-soft" : "border-border bg-surface"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold">{name}</span>
        <StatusPill busy={busy} />
      </div>
      {busy ? (
        <div className="mt-3 flex items-baseline justify-between text-sm">
          <span className="font-mono text-muted">{call.caller_id}</span>
          <span data-testid="duration" className="font-mono text-lg font-semibold tabular-nums">
            {hhmmss(secs)}
          </span>
        </div>
      ) : (
        <div className="mt-3 text-sm text-muted">Waiting for a call</div>
      )}
    </div>
  );
}

function Skeleton() {
  return (
    <div aria-busy="true" aria-label="Loading live data" className="animate-pulse space-y-4">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => <div key={i} className="h-24 rounded-xl bg-surface-2" />)}
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {[0, 1, 2].map((i) => <div key={i} className="h-24 rounded-xl bg-surface-2" />)}
      </div>
    </div>
  );
}

/** The at-a-glance page: who is on a call, with whom, for how long, and
 *  whether anything is wrong. */
export function LivePage({ state, status }: { state: LiveState | null; status: LiveStatus }) {
  const now = useNow(1000);
  if (!state) {
    return (
      <>
        <PageHeader title="Live" />
        <Skeleton />
      </>
    );
  }
  const { pool, counters: c, calls } = state;
  const problems = c.calls_failed_total + c.calls_rejected_total;
  const audioIssues = c.frames_dropped_total + c.pacer_slips_total;
  const pct = pool.capacity ? Math.round((pool.busy / pool.capacity) * 100) : 0;

  return (
    <>
      <PageHeader
        title="Live"
        subtitle="Right now, across every agent"
        right={
          audioIssues ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-bad-soft px-3 py-1 text-sm font-medium text-bad">
              <AlertTriangle size={16} aria-hidden="true" /> {audioIssues} audio issues
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-ok-soft px-3 py-1 text-sm font-medium text-ok">
              <CheckCircle2 size={16} aria-hidden="true" /> Audio OK
            </span>
          )
        }
      />

      {status === "disconnected" && (
        <div role="status" className="mb-4 flex items-center gap-2 rounded-lg border border-bad/40 bg-bad-soft px-4 py-3 text-sm text-bad">
          <WifiOff size={16} aria-hidden="true" />
          Offline — reconnecting. The numbers below may be out of date.
        </div>
      )}

      <div className="mb-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Kpi label="Agents on call" value={`${pool.busy} / ${pool.capacity}`}>
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-2" aria-hidden="true">
            <div className="h-full rounded-full bg-busy" style={{ width: `${pct}%` }} />
          </div>
        </Kpi>
        <Kpi label="Answered" value={c.calls_total} sub="since the last restart" />
        <Kpi label="Transferred" value={c.transfers_total} sub="to a department" />
        <Kpi
          label="Problems"
          value={problems}
          warn={problems > 0}
          sub={`${c.calls_failed_total} failed · ${c.calls_rejected_total} turned away`}
        />
      </div>

      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted">Agents</h2>
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {agentsWithCalls(state).map((a) => <AgentCard key={a.name} name={a.name} call={a.call} now={now} />)}
      </div>

      <Card title="Calls in progress">
        {calls.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted">No calls right now</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-muted">
                <th className="pb-2 font-medium">Duration</th>
                <th className="pb-2 font-medium">Agent</th>
                <th className="pb-2 font-medium">Caller</th>
                <th className="pb-2 font-medium">Call</th>
              </tr>
            </thead>
            <tbody>
              {calls.map((call) => (
                <tr key={call.call_id} className="border-t border-border">
                  <td className="py-2 font-mono tabular-nums">
                    {hhmmss((now - new Date(call.started_at).getTime()) / 1000)}
                  </td>
                  <td className="py-2">{call.persona}</td>
                  <td className="py-2 font-mono">{call.caller_id}</td>
                  <td className="py-2 font-mono text-muted">{call.call_id.slice(0, 8)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
