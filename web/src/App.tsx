import { useCallback, useState } from "react";
import { Shell } from "./components/Shell";
import { AgentsPage } from "./pages/AgentsPage";
import { CallsPage } from "./pages/CallsPage";
import { LivePage } from "./pages/LivePage";
import { useRoute } from "./router";
import { useLive } from "./useLive";

// A call's record is written a moment AFTER it leaves the live list (bot.py
// removes it from the live list first), so the list is reloaded after this
// delay -- reloading at once would miss the very call that triggered it.
export const HISTORY_REFRESH_DELAY_MS = 1500;

/** The supervisor app: one live connection shared by every page
 *  ([[decisions]] 056, 057). */
export function App() {
  const route = useRoute();
  const [refreshKey, setRefreshKey] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const onCallsEnded = useCallback(() => {
    setTimeout(() => setRefreshKey((k) => k + 1), HISTORY_REFRESH_DELAY_MS);
  }, []);
  const { state, status } = useLive(onCallsEnded);
  const agents = state ? [...state.pool.free_agents, ...state.pool.busy_agents].sort() : [];

  return (
    <Shell route={route} tenant={state?.tenant ?? ""} status={status} uptime={state?.counters.uptime_s ?? null}>
      {route === "live" && <LivePage state={state} status={status} />}
      {route === "calls" && (
        <CallsPage agents={agents} refreshKey={refreshKey} selected={selected} onSelect={setSelected} />
      )}
      {route === "agents" && <AgentsPage state={state} />}
    </Shell>
  );
}
