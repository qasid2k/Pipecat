import { hhmmss, useNow } from "../format";
import type { LiveCall } from "../types";

/** Calls in progress. Durations tick locally from `started_at`. */
export function LiveCalls({ calls }: { calls: LiveCall[] }) {
  const now = useNow(1000);
  return (
    <div className="panel">
      <h2>Calls in progress</h2>
      <table>
        <thead>
          <tr><th>Duration</th><th>Agent</th><th>Caller</th><th>Call id</th></tr>
        </thead>
        <tbody>
          {calls.map((c) => (
            <tr key={c.call_id}>
              <td className="dur">{hhmmss((now - new Date(c.started_at).getTime()) / 1000)}</td>
              <td>{c.persona}</td>
              <td className="mono">{c.caller_id}</td>
              <td className="mono">{c.call_id.slice(0, 8)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {calls.length === 0 && <div className="empty">No calls in progress</div>}
    </div>
  );
}
