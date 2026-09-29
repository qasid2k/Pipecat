import { useCallback, useState } from "react";
import { CallDetail } from "./components/CallDetail";
import { LiveCalls } from "./components/LiveCalls";
import { RecentCalls } from "./components/RecentCalls";
import { Header, Summary } from "./components/Summary";
import { hhmmss } from "./format";
import { useLive } from "./useLive";

// A call's record is written a moment AFTER it leaves the live list (bot.py
// removes it from the live list first), so the list is reloaded after this
// delay -- reloading at once would miss the very call that triggered it.
export const HISTORY_REFRESH_DELAY_MS = 1500;

/** The supervisor dashboard. The same screens as the old single HTML file,
 *  rebuilt as components ([[decisions]] 056). */
export function App() {
  const [refreshKey, setRefreshKey] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const onCallsEnded = useCallback(() => {
    setTimeout(() => setRefreshKey((k) => k + 1), HISTORY_REFRESH_DELAY_MS);
  }, []);
  const { state, status } = useLive(onCallsEnded);

  const agents = state ? [...state.pool.free_agents, ...state.pool.busy_agents].sort() : [];
  return (
    <>
      <Header tenant={state?.tenant ?? ""} status={status} />
      <Summary state={state} />
      <LiveCalls calls={state?.calls ?? []} />
      <RecentCalls agents={agents} refreshKey={refreshKey} selected={selected} onSelect={setSelected} />
      {selected && <CallDetail callId={selected} />}
      <footer>{state ? "up " + hhmmss(state.counters.uptime_s) : ""}</footer>
    </>
  );
}
