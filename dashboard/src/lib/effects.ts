/**
 * EffectLedger receipt queue (D23 / P13, rendered by D24 / P14).
 *
 * The endpoint answers one status at a time, keyed by effect id: `status=UNKNOWN`
 * is the actionable queue, `status=RECONCILING&stale=1` is "the reconciler died".
 * Nothing here mutates — reconciling stays with the operator CLI.
 */

export interface EffectReceipt {
  effect_id: string;
  kind: string;
  status: string;
  owner: string;
  lease_until: number;
  lease_expired: boolean;
  updated_at: number;
  reason: string;
  request: Record<string, unknown>;
}

export interface EffectPagePayload {
  effects?: EffectReceipt[];
  count?: number;
  total?: number;
  next_after?: string | null;
}

export interface EffectQueueState {
  rows: EffectReceipt[];
  total: number;
  nextAfter: string | null;
}

/** Statuses the endpoint accepts, in the order the panel offers them. */
export const EFFECT_STATUSES = [
  "UNKNOWN",
  "RECONCILING",
  "IN_FLIGHT",
  "PREPARED",
  "CONFIRMED",
  "FAILED",
] as const;

export const EFFECT_STALE_STATUS = "RECONCILING";

/** Query for one page of the queue. `stale` only means anything for RECONCILING. */
export function effectsQuery(
  options: {
    status?: string;
    stale?: boolean;
    limit?: number;
    after?: string | null;
  } = {},
): string {
  const status = options.status ?? "UNKNOWN";
  const limit = options.limit ?? 100;
  const parts = [`status=${status}`, `limit=${limit}`];
  if (options.stale) parts.push("stale=1");
  if (options.after) parts.push(`after=${encodeURIComponent(options.after)}`);
  return `/dashboard/api/effects?${parts.join("&")}`;
}

/** `3m` / `2h` / `5d`; empty for a missing timestamp. */
export function effectAge(updatedAt: number, now: number = Date.now() / 1000): string {
  if (!updatedAt) return "";
  const seconds = Math.max(0, Math.round(now - updatedAt));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

/**
 * Who holds the receipt and for how long: an expired lease is the interesting case
 * (D23's `stale` queue), so it says so instead of printing a past deadline.
 */
export function effectOwnerLine(receipt: EffectReceipt): string {
  if (receipt.lease_expired) {
    return `${receipt.owner || "nobody"} · lease expired`;
  }
  if (!receipt.lease_until) return receipt.owner || "";
  const remaining = Math.max(0, Math.round(receipt.lease_until - Date.now() / 1000));
  return `${receipt.owner || "?"} · lease ${remaining}s`;
}

/**
 * The one-line scope a receipt carries: a branch, the MetaInfer operation, or the
 * branch set a merge published. Values are ids the operator already knows.
 */
export function effectRequestLine(request: Record<string, unknown> | undefined): string {
  if (!request) return "";
  const parts: string[] = [];
  const branch = request.branch;
  if (typeof branch === "string" && branch) parts.push(`branch ${branch}`);
  const operation = request.operation_id;
  if (typeof operation === "string" && operation) parts.push(`operation ${operation}`);
  const backend = request.backend_id;
  if (typeof backend === "string" && backend) parts.push(`backend ${backend}`);
  const merged = request.merged_branches;
  if (Array.isArray(merged) && merged.length) parts.push(`${merged.length} merged branch(es)`);
  return parts.join(" · ");
}

/** `{UNKNOWN · 2m}` — the status plus how long it has been sitting there. */
export function effectHeadline(receipt: EffectReceipt, now?: number): string {
  const age = effectAge(receipt.updated_at, now);
  return age ? `${receipt.status} · ${age}` : receipt.status;
}

export function effectsFromPayload(payload: EffectPagePayload): EffectQueueState {
  const rows = Array.isArray(payload.effects) ? payload.effects : [];
  return {
    rows,
    total: typeof payload.total === "number" ? payload.total : rows.length,
    nextAfter: typeof payload.next_after === "string" ? payload.next_after : null,
  };
}

/** Append one page, deduped by effect id; the cursor is whatever the endpoint said. */
export function appendEffectPage(
  state: EffectQueueState,
  payload: EffectPagePayload,
): EffectQueueState {
  const page = effectsFromPayload(payload);
  const seen = new Set(state.rows.map((row) => row.effect_id));
  const rows = [...state.rows, ...page.rows.filter((row) => !seen.has(row.effect_id))];
  return { rows, total: page.total, nextAfter: page.nextAfter };
}

/** `2 of 7 UNKNOWN` — never claims the window is the whole queue. */
export function effectCountLine(count: number, total: number, status: string): string {
  return `${count} of ${total} ${status}`;
}
