import { useEffect, useRef, useState } from "react";
import type { LiveState } from "./types";

export const RECONNECT_MS = 2000;

export type LiveStatus = "connecting" | "live" | "disconnected";

/**
 * The live state pushed over `WS /live`, plus whether we are connected.
 *
 * The server pushes only when something changes ([[decisions]] 045). On a
 * close we reconnect after RECONNECT_MS rather than leave a dead page on
 * screen: a dashboard that silently stopped updating looks like a service
 * with no calls, which is worse than one that says "disconnected".
 *
 * `onCallsEnded` fires when a call that was live is no longer in a push -- the
 * moment its record is on its way to the database.
 */
export function useLive(onCallsEnded?: () => void) {
  const [state, setState] = useState<LiveState | null>(null);
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const onEnded = useRef(onCallsEnded);
  onEnded.current = onCallsEnded;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;
    let liveIds = new Set<string>();

    const connect = () => {
      const scheme = location.protocol === "https:" ? "wss://" : "ws://";
      ws = new WebSocket(scheme + location.host + "/live");
      ws.onopen = () => setStatus("live");
      ws.onmessage = (event) => {
        const next: LiveState = JSON.parse(event.data);
        const nextIds = new Set(next.calls.map((c) => c.call_id));
        const ended = [...liveIds].some((id) => !nextIds.has(id));
        liveIds = nextIds;
        setState(next);
        if (ended) onEnded.current?.();
      };
      ws.onclose = () => {
        setStatus("disconnected");
        if (!stopped) retry = setTimeout(connect, RECONNECT_MS);
      };
      ws.onerror = () => ws?.close();
    };

    connect();
    return () => {
      stopped = true;
      if (retry) clearTimeout(retry);
      ws?.close();
    };
  }, []);

  return { state, status };
}
