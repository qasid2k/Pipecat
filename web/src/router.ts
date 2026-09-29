import { useEffect, useState } from "react";

// Hash routing (`/#/calls`), not path routing: `/calls` is already an API
// route on the server, and three pages don't justify a router package
// ([[decisions]] 057).
export const ROUTES = ["live", "calls", "agents"] as const;
export type Route = (typeof ROUTES)[number];

export function parseRoute(hash: string): Route {
  const name = hash.replace(/^#\/?/, "");
  return (ROUTES as readonly string[]).includes(name) ? (name as Route) : "live";
}

export const href = (route: Route) => `#/${route}`;

/** The current page, following the address bar (so back/forward work). */
export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseRoute(location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}
