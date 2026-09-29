import { Activity, History, Menu, Users, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { hhmmss } from "../format";
import { href, type Route } from "../router";
import type { LiveStatus } from "../useLive";

const NAV: { route: Route; label: string; icon: typeof Activity }[] = [
  { route: "live", label: "Live", icon: Activity },
  { route: "calls", label: "Calls", icon: History },
  { route: "agents", label: "Agents", icon: Users },
];

const STATUS: Record<LiveStatus, { label: string; dot: string }> = {
  connecting: { label: "Connecting…", dot: "bg-muted" },
  live: { label: "Live", dot: "bg-ok animate-pulse" },
  disconnected: { label: "Offline — reconnecting", dot: "bg-bad" },
};

function NavLinks({ route, onPick }: { route: Route; onPick?: () => void }) {
  return (
    <nav aria-label="Pages" className="flex flex-col gap-1">
      {NAV.map(({ route: r, label, icon: Icon }) => {
        const current = r === route;
        return (
          <a
            key={r}
            href={href(r)}
            onClick={onPick}
            aria-current={current ? "page" : undefined}
            className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
              current ? "bg-brand-soft text-brand" : "text-muted hover:bg-surface-2 hover:text-text"
            }`}
          >
            <Icon size={18} aria-hidden="true" />
            {label}
          </a>
        );
      })}
    </nav>
  );
}

function ConnectionStatus({ status, uptime }: { status: LiveStatus; uptime: number | null }) {
  const s = STATUS[status];
  return (
    <div className="text-xs text-muted">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${s.dot}`} aria-hidden="true" />
        <span className="font-medium text-text">{s.label}</span>
      </div>
      {uptime !== null && <div className="mt-1 pl-4">Up {hhmmss(uptime)}</div>}
    </div>
  );
}

interface Props {
  route: Route;
  tenant: string;
  status: LiveStatus;
  uptime: number | null;
  children: ReactNode;
}

/** The frame around every page: a sidebar on wide screens, a top bar with a
 *  menu on narrow ones. */
export function Shell({ route, tenant, status, uptime, children }: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const brand = (
    <div>
      <div className="text-base font-semibold tracking-tight">Voice agents</div>
      <div className="text-xs text-muted">{tenant}</div>
    </div>
  );
  return (
    <div className="min-h-screen md:flex">
      <aside className="hidden w-60 shrink-0 flex-col justify-between border-r border-border bg-surface p-4 md:flex md:sticky md:top-0 md:h-screen">
        <div className="flex flex-col gap-6">
          {brand}
          <NavLinks route={route} />
        </div>
        <ConnectionStatus status={status} uptime={uptime} />
      </aside>

      <header className="border-b border-border bg-surface px-4 py-3 md:hidden">
        <div className="flex items-center justify-between">
          {brand}
          <button
            type="button"
            aria-label="Menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((o) => !o)}
            className="rounded-lg p-2 text-muted hover:bg-surface-2"
          >
            {menuOpen ? <X size={20} aria-hidden="true" /> : <Menu size={20} aria-hidden="true" />}
          </button>
        </div>
        {menuOpen && (
          <div className="mt-3 flex flex-col gap-3">
            <NavLinks route={route} onPick={() => setMenuOpen(false)} />
            <ConnectionStatus status={status} uptime={uptime} />
          </div>
        )}
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 p-4 md:p-8">{children}</main>
    </div>
  );
}

/** Page heading + an optional right-hand slot (badges, actions). */
export function PageHeader({ title, subtitle, right }: { title: string; subtitle?: string; right?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-muted">{subtitle}</p>}
      </div>
      {right}
    </div>
  );
}

/** The card every section sits in. */
export function Card({ title, children, className = "" }: { title?: string; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-border bg-surface p-4 md:p-5 ${className}`}>
      {title && <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted">{title}</h2>}
      {children}
    </section>
  );
}

/** Free / On call, as a pill. Always text + colour, never colour alone. */
export function StatusPill({ busy }: { busy: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${
        busy ? "bg-busy-soft text-busy" : "bg-ok-soft text-ok"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${busy ? "bg-busy" : "bg-ok"}`} aria-hidden="true" />
      {busy ? "On call" : "Free"}
    </span>
  );
}
