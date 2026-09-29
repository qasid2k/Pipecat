import { useCallback, useEffect, useState } from "react";
import { fetchHistory } from "../api";
import { hhmmss } from "../format";
import { NO_FILTERS, type CallRow, type Filters } from "../types";

interface Props {
  agents: string[];
  /** Bumped by the app when a live call ends, to reload the list. */
  refreshKey: number;
  selected: string | null;
  onSelect: (callId: string) => void;
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
    <div className="panel" style={{ marginTop: 16 }}>
      <h2>Recent calls</h2>
      <form className="filters" onSubmit={(e) => { e.preventDefault(); setApplied(draft); }}>
        <label>from <input type="date" id="f-since" value={draft.since} onChange={set("since")} /></label>
        <label>to <input type="date" id="f-until" value={draft.until} onChange={set("until")} /></label>
        <label>agent{" "}
          <select id="f-agent" value={draft.persona} onChange={set("persona")}>
            <option value="">any</option>
            {agents.map((a) => <option key={a}>{a}</option>)}
          </select>
        </label>
        <label>outcome{" "}
          <select id="f-outcome" value={draft.outcome} onChange={set("outcome")}>
            <option value="">any</option>
            <option value="transferred">transferred</option>
            <option value="handled">handled by the agent</option>
          </select>
        </label>
        <label>caller{" "}
          <input type="search" id="f-caller" placeholder="number contains…" size={14}
                 value={draft.caller} onChange={set("caller")} />
        </label>
        <button type="submit">Filter</button>
        <button type="button" onClick={() => { setDraft(NO_FILTERS); setApplied(NO_FILTERS); }}>
          Clear
        </button>
      </form>
      <table>
        <thead>
          <tr><th>Started</th><th>Length</th><th>Agent</th><th>Caller</th><th>How it ended</th><th>Transferred to</th></tr>
        </thead>
        <tbody id="history">
          {rows.map((c) => (
            <tr key={c.call_id} className={c.call_id === selected ? "sel" : ""} onClick={() => onSelect(c.call_id)}>
              <td className="mono">{new Date(c.started_at).toLocaleString()}</td>
              <td className="dur">{hhmmss(c.duration_s || 0)}</td>
              <td>{c.persona}</td>
              <td className="mono">{c.caller_id}</td>
              <td>{c.cause || c.end_reason}</td>
              <td>{c.transferred_to || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && <div className="empty">Call history is unavailable: {error}</div>}
      {!error && rows.length === 0 && (
        <div className="empty">{filtered ? "No calls match these filters" : "No finished calls yet"}</div>
      )}
    </div>
  );
}
