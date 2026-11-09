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
  | "inference"
  | "attention"
  | "approvals"
  | "effects";

export const TERMINAL_VIEWS: readonly TerminalView[] = [
  "overview",
  "tasks",
  "workers",
  "search",
  "memory",
  "logs",
  "inference",
  "attention",
  "approvals",
  "effects",
] as const;

const DASHBOARD_PREFIX = "/dashboard";

/**
 * Single-letter view shortcuts, kept next to the view list so the key handler and
 * the `?` help sheet read the same table. A view without a letter is still
 * reachable by number.
 */
export const LETTER_VIEWS: Readonly<Record<string, TerminalView>> = {
  o: "overview",
  t: "tasks",
  w: "workers",
  s: "search",
  m: "memory",
  e: "logs",
  i: "inference",
  a: "approvals",
};

/**
 * Human hint for the numeric view keys, derived from the actual keys.
 *
 * The dashboard used to hardcode "1–7" in two places and "1–9" in a third; all
 * three were wrong once a view was added (D24 put receipts on `0`). Deriving the
 * hint from the key list means adding a view cannot leave the hint lying.
 */
export function viewKeyHint(keys: readonly string[]): string {
  const numbers = [...new Set(keys.map(Number))].filter((value) => Number.isInteger(value));
  if (numbers.length === 0) return "";
  numbers.sort((left, right) => left - right);
  const lowest = numbers[0]!;
  const contiguous = numbers.every((value, index) => value === lowest + index);
  return contiguous ? `${lowest}–${numbers[numbers.length - 1]}` : numbers.join("·");
}

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
