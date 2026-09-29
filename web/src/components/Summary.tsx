import type { LiveState } from "../types";
import type { LiveStatus } from "../useLive";

const STATUS: Record<LiveStatus, [string, string]> = {
  connecting: ["connecting…", ""],
  live: ["live", "up"],
  disconnected: ["disconnected", "down"],
};

export function Header({ tenant, status }: { tenant: string; status: LiveStatus }) {
  const [label, cls] = STATUS[status];
  return (
    <header>
      <h1>Voice agents</h1>
      <span className="tenant">{tenant}</span>
      <span id="conn" className={cls}>{label}</span>
    </header>
  );
}

function Stat({ label, value, warn = false }: { label: string; value: number | string; warn?: boolean }) {
  return (
    <div className={warn ? "stat warn" : "stat"}>
      <small>{label}</small>
      <b>{value}</b>
    </div>
  );
}

/** The four panels across the top: capacity, agents, totals, audio health. */
export function Summary({ state }: { state: LiveState | null }) {
  const dash = "–";
  const pool = state?.pool;
  const c = state?.counters;
  return (
    <div className="grid" style={{ marginBottom: 16 }}>
      <div className="panel">
        <h2>Capacity</h2>
        <div className="big">
          {pool ? pool.busy : dash}
          <small> / {pool ? pool.capacity : dash} in use</small>
        </div>
      </div>
      <div className="panel">
        <h2>Agents</h2>
        <div className="agents">
          {pool?.busy_agents.map((n) => <span key={n} className="agent busy">{n}</span>)}
          {pool?.free_agents.map((n) => <span key={n} className="agent free">{n}</span>)}
        </div>
      </div>
      <div className="panel">
        <h2>Since start</h2>
        <div className="stats">
          <Stat label="answered" value={c ? c.calls_total : dash} />
          <Stat label="transferred" value={c ? c.transfers_total : dash} />
          {/* Anything non-zero in these is worth a supervisor's attention. */}
          <Stat label="turned away" value={c ? c.calls_rejected_total : dash} warn={!!c?.calls_rejected_total} />
          <Stat label="failed" value={c ? c.calls_failed_total : dash} warn={!!c?.calls_failed_total} />
        </div>
      </div>
      <div className="panel">
        <h2>Audio health</h2>
        <div className="stats">
          <Stat label="frames dropped" value={c ? c.frames_dropped_total : dash} warn={!!c?.frames_dropped_total} />
          <Stat label="pacer slips" value={c ? c.pacer_slips_total : dash} warn={!!c?.pacer_slips_total} />
        </div>
      </div>
    </div>
  );
}
