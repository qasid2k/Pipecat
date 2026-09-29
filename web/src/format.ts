import { useEffect, useState } from "react";

/** 75 -> "1:15", 3725 -> "1:02:05". */
export function hhmmss(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return m >= 60 ? `${Math.floor(m / 60)}:${pad(m % 60)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
}

/** The current time, updated every `ms`. Durations tick on the BROWSER's clock
 *  so the server sends nothing while idle ([[decisions]] 045). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}
