/**
 * Dashboard view <-> URL hash mapping.
 *
 * The operations dashboard keeps its tab state in React, but every view is also
 * addressable through the hash so it can be deep-linked, bookmarked, restored on
 * reload, and navigated with the browser's back/forward buttons:
 *
 *   #/dashboard          -> overview
 *   #/dashboard/tasks    -> tasks
 *   #/dashboard/workers  -> workers
 *   ...
 *
 * Unknown sub-views return null so callers can fall back to their default.
 */

export type TerminalView =
  | "overview"
  | "tasks"
  | "workers"
  | "search"
  | "memory"
  | "logs"
  | "inference";

export const TERMINAL_VIEWS: readonly TerminalView[] = [
  "overview",
  "tasks",
  "workers",
  "search",
  "memory",
  "logs",
  "inference",
] as const;

const DASHBOARD_PREFIX = "/dashboard";

/** Extract the view encoded in a location hash, or null when it is not a dashboard view. */
export function viewFromHash(hash: string): TerminalView | null {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  if (raw !== DASHBOARD_PREFIX && !raw.startsWith(`${DASHBOARD_PREFIX}/`)) return null;
  const part = raw.slice(DASHBOARD_PREFIX.length).replace(/^\/+|\/+$/g, "");
  if (!part) return "overview";
  return (TERMINAL_VIEWS as readonly string[]).includes(part) ? (part as TerminalView) : null;
}

/** Hash for a view; the overview keeps the short `#/dashboard` form. */
export function hashForView(view: TerminalView): string {
  return view === "overview" ? `#${DASHBOARD_PREFIX}` : `#${DASHBOARD_PREFIX}/${view}`;
}
