import { Card, PageHeader, StatusPill } from "../components/Shell";
import { hhmmss, useNow } from "../format";
import type { LiveState } from "../types";
import { agentsWithCalls } from "./LivePage";

/** The roster. A plain list for now; per-agent statistics are slice 5 of
 *  [[backlog]] IMP-004. */
export function AgentsPage({ state }: { state: LiveState | null }) {
  const now = useNow(1000);
  const agents = state ? agentsWithCalls(state) : [];
  return (
    <>
      <PageHeader title="Agents" subtitle={state ? `${state.pool.capacity} agents in the roster` : undefined} />
      <Card>
        {!state ? (
          <p aria-busy="true" className="py-6 text-center text-sm text-muted">Loading…</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-muted">
                <th className="pb-2 font-medium">Agent</th>
                <th className="pb-2 font-medium">Status</th>
                <th className="pb-2 font-medium">Current call</th>
              </tr>
            </thead>
            <tbody>
              {agents.map(({ name, call }) => (
                <tr key={name} className="border-t border-border">
                  <td className="py-3 font-medium">{name}</td>
                  <td className="py-3"><StatusPill busy={call !== null} /></td>
                  <td className="py-3 font-mono text-muted">
                    {call ? `${call.caller_id} · ${hhmmss((now - new Date(call.started_at).getTime()) / 1000)}` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
