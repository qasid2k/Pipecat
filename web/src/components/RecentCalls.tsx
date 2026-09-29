import { useCallback, useEffect, useState } from "react";
import { fetchHistory } from "../api";
import { hhmmss } from "../format";
import { NO_FILTERS, type CallRow, type Filters } from "../types";
import { Card } from "./Shell";

interface Props {
  agents: string[];
  /** Bumped by the app when a live call ends, to reload the list. */
  refreshKey: number;
  selected: string | null;
  onSelect: (callId: string) => void;
}

const FIELD =
  "rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm text-text placeholder:text-muted";

function Label({ text, children }: { text: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs font-medium text-muted">
      {text}
      {children}
    </label>
  );
}

/** Finished calls from the database, with the filter bar. Loaded on open and
 *  when `refreshKey` changes -- never on a timer. */
export function RecentCalls({ agents, refreshKey, selected, onSelect }: Props) {
  const [draft, setDraft] = useState<Filters>(NO_FILTERS);
  const [applied, setApplied] = useState<Filters>(NO_FILTERS);
  const [rows, setRows] = useState<CallRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (filters: Filters) => {
    try {
      setRows(await fetchHistory(filters));
      setError(null);
    } catch (e) {
      setRows([]);
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => { load(applied); }, [load, applied, refreshKey]);

  const set = (key: keyof Filters) => (e: { target: { value: string } }) =>
    setDraft({ ...draft, [key]: e.target.value });
  const filtered = Object.values(applied).some(Boolean);

  return (
    <Card title="Recent calls">
      <form
        className="mb-4 flex flex-wrap items-end gap-3"
        onSubmit={(e) => { e.preventDefault(); setApplied(draft); }}
      >
        <Label text="From"><input type="date" id="f-since" className={FIELD} value={draft.since} onChange={set("since")} /></Label>
        <Label text="To"><input type="date" id="f-until" className={FIELD} value={draft.until} onChange={set("until")} /></Label>
        <Label text="Agent">
          <select id="f-agent" className={FIELD} value={draft.persona} onChange={set("persona")}>
            <option value="">Any</option>
            {agents.map((a) => <option key={a}>{a}</option>)}
          </select>
        </Label>
        <Label text="Outcome">
          <select id="f-outcome" className={FIELD} value={draft.outcome} onChange={set("outcome")}>
            <option value="">Any</option>
            <option value="transferred">Transferred</option>
            <option value="handled">Handled by the agent</option>
          </select>
        </Label>
        <Label text="Caller">
          <input type="search" id="f-caller" className={FIELD} placeholder="Number contains…" size={14}
                 value={draft.caller} onChange={set("caller")} />
        </Label>
        <div className="flex gap-2">
          <button type="submit" className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-on-brand hover:opacity-90">
            Filter
          </button>
          <button type="button" className="rounded-lg border border-border px-3 py-1.5 text-sm font-medium text-muted hover:bg-surface-2"
                  onClick={() => { setDraft(NO_FILTERS); setApplied(NO_FILTERS); }}>
            Clear
          </button>
        </div>
      </form>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-muted">
              <th className="pb-2 pr-3 font-medium">Started</th>
              <th className="pb-2 pr-3 font-medium">Length</th>
              <th className="pb-2 pr-3 font-medium">Agent</th>
              <th className="pb-2 pr-3 font-medium">Caller</th>
              <th className="pb-2 pr-3 font-medium">How it ended</th>
              <th className="pb-2 font-medium">Transferred to</th>
            </tr>
          </thead>
          <tbody id="history">
            {rows.map((c) => (
              <tr
                key={c.call_id}
                tabIndex={0}
                aria-selected={c.call_id === selected}
                onClick={() => onSelect(c.call_id)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(c.call_id); } }}
                className={`cursor-pointer border-t border-border hover:bg-surface-2 ${c.call_id === selected ? "bg-brand-soft" : ""}`}
              >
                <td className="py-2 pr-3 whitespace-nowrap">{new Date(c.started_at).toLocaleString()}</td>
                <td className="py-2 pr-3 font-mono tabular-nums">{hhmmss(c.duration_s || 0)}</td>
                <td className="py-2 pr-3">{c.persona}</td>
                <td className="py-2 pr-3 font-mono">{c.caller_id}</td>
                <td className="py-2 pr-3 text-muted">{c.cause || c.end_reason}</td>
                <td className="py-2">
                  {c.transferred_to
                    ? <span className="rounded-full bg-brand-soft px-2 py-0.5 text-xs font-medium text-brand">{c.transferred_to}</span>
                    : <span className="text-muted">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {error && <p className="py-6 text-center text-sm text-bad">Call history is unavailable: {error}</p>}
      {!error && rows.length === 0 && (
        <p className="py-6 text-center text-sm text-muted">
          {filtered ? "No calls match these filters" : "No finished calls yet"}
        </p>
      )}
    </Card>
  );
}
