/**
 * Governance attention ("todo" worklist) HTTP client.
 *
 * The dashboard HTTP surface for the governance plane is served by
 * `python/ultimate_coders/dashboard/attention_api.py` (`/api/...`, mounted
 * without a prefix):
 *
 *   GET  /api/attention                 → `{ items: […], capacity: {…} }`
 *   POST /api/attention/{id}/resolve    → `{ item: {…} }`
 *
 * The gateway (Rust) owns attention state; this module only reads the
 * projection. Three wire facts are pinned here because the panel must not
 * guess them:
 *
 * 1. `since` / `resolved_at` arrive as RFC 3339 strings
 *    (`AttentionItem.to_dict`). A number is also accepted as Unix *seconds*
 *    (below the milliseconds ceiling): the same field has been an int64 on the
 *    proto-shaped wire, and a worklist that sorted on `undefined` would
 *    mis-order silently.
 * 2. The canonical order is human decisions first, then priority descending,
 *    then the oldest `since`, then `case_id` (`id` breaks the last tie) —
 *    mirrored from `AttentionItem.worklist_order_key`, so two refreshes never
 *    reshuffle the worklist.
 * 3. `503 {"error": "governance_unavailable"}` means the NATS bridge is down,
 *    which is an operational state, not a failed request. Every helper here
 *    therefore *resolves* to a discriminated result instead of rejecting, so a
 *    dead bridge cannot turn the panel's poll loop into a toast storm.
 *
 * Junk is filtered, never thrown: one future-gateway record must not blind the
 * operator to every other pending decision (the rule `attention_api.py`
 * applies server-side too).
 *
 * The worklist reply already carries the capacity snapshot, so the panel never
 * needs a second round trip against `/api/capacity`.
 */

/** Wire tokens for "why this case is not progressing" (Rust/Python enum). */
export const ATTENTION_KINDS = [
  "needs_human",
  "waiting_event",
  "blocked",
  "verifying",
] as const;

export type AttentionKind = (typeof ATTENTION_KINDS)[number];

/** Resolution kinds the gateway accepts for `attention.resolve`. */
export const RESOLUTION_KINDS = [
  "human_decision",
  "event_received",
  "automatic",
  "case_closed",
] as const;

export type ResolutionKind = (typeof RESOLUTION_KINDS)[number];

/** Highest priority an item may carry (mirrors the Rust `u8`). */
export const MAX_PRIORITY = 255;

/** Default page size for the worklist (the endpoint's own default is 100). */
export const ATTENTION_PAGE_LIMIT = 100;

/** One worklist entry, normalised for rendering. */
export interface AttentionItem {
  id: string;
  case_id: string;
  kind: AttentionKind;
  summary: string;
  /** Epoch milliseconds; `0` when the wire timestamp was unusable. */
  since: number;
  /** `0`–`255`; higher is more urgent (mirrors `AttentionItem.priority`). */
  priority: number;
  task_id?: string;
  detail?: string;
  human_action?: string;
  /** Epoch milliseconds; present only for an already-resolved item. */
  resolved_at?: number;
  resolution_summary?: string;
}

/** Capacity snapshot (`Capacity.to_dict`) — every field is a non-negative int. */
export interface Capacity {
  worker_slots_total: number;
  worker_slots_busy: number;
  worker_slots_reserved_human: number;
  waiting_event_cases: number;
  verifying_cases: number;
  needs_human_items: number;
  blocked_cases: number;
  worker_slots_available: number;
  slots_saved_by_quiet_waiting: number;
}

export interface AttentionSnapshot {
  items: AttentionItem[];
  capacity: Capacity;
}

export interface ResolveRequest {
  resolution_kind: ResolutionKind;
  by?: string;
  detail?: string;
}

/** Result of one call: a dead bridge is data, not an exception. */
export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: string };

/** Sentinel used for the documented "bridge down" status. */
export const GOVERNANCE_OFFLINE_STATUS = 503;

/**
 * Same-origin prefix. The governance routers answer under `/api` and are
 * mounted without a prefix; `VITE_GOVERNANCE_API_BASE` exists for a deployment
 * that fronts them under another prefix (e.g. behind the same nginx location
 * the rest of the dashboard API uses).
 */
const DEFAULT_API_BASE = "";

const ENV = import.meta.env as
  | { VITE_GOVERNANCE_API_BASE?: string }
  | undefined;

/** Bearer token stored by the dashboard login (`hooks/useAuth.ts`). */
const TOKEN_STORAGE_KEY = "uc_dashboard_token";

function apiBase(): string {
  const configured = ENV?.VITE_GOVERNANCE_API_BASE?.trim();
  if (!configured) return DEFAULT_API_BASE;
  return configured.replace(/\/+$/, "");
}

/** Absolute (same-origin) URL for a governance path. */
export function attentionUrl(path: string): string {
  return `${apiBase()}${path.startsWith("/") ? path : `/${path}`}`;
}

/** Read the stored bearer token without importing the gRPC-backed auth hook. */
function authHeaders(): Record<string, string> {
  try {
    const token = globalThis.localStorage?.getItem(TOKEN_STORAGE_KEY);
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    // localStorage can be unavailable (private mode, SSR); reads are open
    // without a token and a token-gated deployment answers 401, not a crash.
    return {};
  }
}

function describe(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

export function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

/** The endpoint's own error token (`{"error": "…"}`), when it sent one. */
function errorToken(payload: unknown): string | null {
  const record = asRecord(payload);
  const token = record?.error;
  return typeof token === "string" && token.trim() ? token.trim() : null;
}

/**
 * One JSON call. Never rejects: transport failures and non-2xx answers both
 * come back as `{ok: false}` — a thrown error in a poll loop becomes a toast
 * per poll, which is exactly the storm the offline state exists to prevent.
 */
export async function requestJson(
  path: string,
  init: { method?: string; body?: unknown } = {},
): Promise<ApiResult<unknown>> {
  const { method = "GET", body } = init;
  let response: Response;
  try {
    response = await fetch(attentionUrl(path), {
      method,
      headers: {
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...authHeaders(),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (cause) {
    // A transport failure (bridge restart, offline UI image) is the same
    // operational state as the documented 503; status 0 keeps the cause honest.
    return { ok: false, status: 0, error: describe(cause) };
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    return {
      ok: false,
      status: response.status,
      error: errorToken(payload) ?? `http_${response.status}`,
    };
  }
  if (payload === null || typeof payload !== "object") {
    return { ok: false, status: 502, error: "invalid_payload" };
  }
  return { ok: true, data: payload };
}

function requiredText(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

export function optionalText(value: unknown): string | undefined {
  const text = requiredText(value);
  return text === null ? undefined : text;
}

/** Coerce a wire counter to a non-negative integer (junk becomes `0`). */
export function asSlotCount(value: unknown): number {
  if (typeof value === "boolean") return value ? 1 : 0;
  const numeric =
    typeof value === "number"
      ? value
      : typeof value === "string" && value.trim()
        ? Number(value)
        : Number.NaN;
  if (!Number.isFinite(numeric)) return 0;
  return Math.max(0, Math.trunc(numeric));
}

/** `1_000_000_000_000` ms is 2001; anything smaller is Unix seconds. */
const UNIX_SECONDS_CEILING = 1e11;

/**
 * Normalise a wire timestamp to epoch milliseconds; `0` when unusable.
 *
 * `since` is RFC 3339 upstream. A numeric value is read as Unix seconds below
 * the milliseconds ceiling, so an older int64 wire still sorts instead of
 * collapsing to the epoch.
 */
export function epochMillis(value: unknown): number {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return 0;
    const seconds = Math.abs(value) < UNIX_SECONDS_CEILING;
    return Math.max(0, Math.round(seconds ? value * 1000 : value));
  }
  if (typeof value !== "string" || !value.trim()) return 0;
  const text = value.trim();
  // A bare number in a string field is the same int64 wire, not a date.
  if (/^\d+(?:\.\d+)?$/.test(text)) return epochMillis(Number(text));
  const parsed = Date.parse(text);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
}

function optionalEpochMillis(value: unknown): number | undefined {
  if (value === undefined || value === null) return undefined;
  const parsed = epochMillis(value);
  // Mirrors `AttentionItem.from_dict`: an unparseable `resolved_at` is "not
  // resolved", not "resolved at the epoch".
  return parsed > 0 ? parsed : undefined;
}

function parseKind(value: unknown): AttentionKind | null {
  if (typeof value !== "string") return null;
  const token = value.trim().toLowerCase();
  return (ATTENTION_KINDS as readonly string[]).includes(token)
    ? (token as AttentionKind)
    : null;
}

/**
 * Project one wire record, or `null` when it cannot be shown.
 *
 * `id` and `case_id` are required (an entry without an identity cannot be
 * resolved); an unknown `kind` is dropped so an item the UI cannot classify
 * never renders in the wrong section.
 */
export function parseAttentionItem(value: unknown): AttentionItem | null {
  const record = asRecord(value);
  if (!record) return null;
  const id = requiredText(record.id);
  const caseId = requiredText(record.case_id);
  const kind = parseKind(record.kind);
  if (id === null || caseId === null || kind === null) return null;

  const item: AttentionItem = {
    id,
    case_id: caseId,
    kind,
    summary: optionalText(record.summary) ?? "",
    since: epochMillis(record.since),
    priority: Math.min(asSlotCount(record.priority), MAX_PRIORITY),
  };
  const taskId = optionalText(record.task_id);
  if (taskId !== undefined) item.task_id = taskId;
  const detail = optionalText(record.detail);
  if (detail !== undefined) item.detail = detail;
  const humanAction = optionalText(record.human_action);
  if (humanAction !== undefined) item.human_action = humanAction;
  const resolvedAt = optionalEpochMillis(record.resolved_at);
  if (resolvedAt !== undefined) item.resolved_at = resolvedAt;
  const resolutionSummary = optionalText(record.resolution_summary);
  if (resolutionSummary !== undefined) item.resolution_summary = resolutionSummary;
  return item;
}

/**
 * Project a wire list, filtering unusable entries (never throws).
 *
 * Already-resolved items are dropped unless `includeResolved`, because the
 * worklist answers "what still needs something"; a stale cached reply must not
 * resurrect a decision that has been recorded.
 */
export function parseAttentionItems(
  value: unknown,
  includeResolved = false,
): AttentionItem[] {
  if (!Array.isArray(value)) return [];
  const items: AttentionItem[] = [];
  for (const entry of value) {
    const item = parseAttentionItem(entry);
    if (item === null) continue;
    if (!includeResolved && item.resolved_at !== undefined) continue;
    items.push(item);
  }
  return items;
}

/** The all-zero capacity, used when a reply carries no usable snapshot. */
export function emptyCapacity(): Capacity {
  return {
    worker_slots_total: 0,
    worker_slots_busy: 0,
    worker_slots_reserved_human: 0,
    waiting_event_cases: 0,
    verifying_cases: 0,
    needs_human_items: 0,
    blocked_cases: 0,
    worker_slots_available: 0,
    slots_saved_by_quiet_waiting: 0,
  };
}

/**
 * Project the capacity snapshot, coercing every counter to a non-negative int.
 *
 * The derived fields are recomputed, never copied: a stale or partial reply
 * must not be able to make them disagree with the counters it shipped (the
 * rule `Capacity.from_dict` applies server-side). `worker_slots_available`
 * therefore never reports a negative number.
 */
export function parseCapacity(value: unknown): Capacity {
  const record = asRecord(value);
  const capacity: Capacity = {
    worker_slots_total: asSlotCount(record?.worker_slots_total),
    worker_slots_busy: asSlotCount(record?.worker_slots_busy),
    worker_slots_reserved_human: asSlotCount(record?.worker_slots_reserved_human),
    waiting_event_cases: asSlotCount(record?.waiting_event_cases),
    verifying_cases: asSlotCount(record?.verifying_cases),
    needs_human_items: asSlotCount(record?.needs_human_items),
    blocked_cases: asSlotCount(record?.blocked_cases),
    worker_slots_available: 0,
    slots_saved_by_quiet_waiting: 0,
  };
  capacity.worker_slots_available = Math.max(
    0,
    capacity.worker_slots_total -
      capacity.worker_slots_busy -
      capacity.worker_slots_reserved_human,
  );
  capacity.slots_saved_by_quiet_waiting =
    capacity.waiting_event_cases + capacity.verifying_cases;
  return capacity;
}

/** True when the failure means "the governance plane is not reachable". */
export function isOffline(result: ApiResult<unknown>): boolean {
  return !result.ok && (result.status === GOVERNANCE_OFFLINE_STATUS || result.status === 0);
}

function humanRank(item: AttentionItem): number {
  return item.kind === "needs_human" ? 0 : 1;
}

/** Ordinal (locale-independent) string compare, matching the Python sort. */
function compareText(a: string, b: string): number {
  if (a === b) return 0;
  return a < b ? -1 : 1;
}

/**
 * Deterministic operator-worklist order.
 *
 * Human decisions first, then priority descending, then the oldest `since`,
 * then `case_id` (`id` breaks the last tie). Mirrors
 * `AttentionItem.worklist_order_key`; the input array is never mutated.
 */
export function worklistOrder(items: readonly AttentionItem[]): AttentionItem[] {
  return [...items].sort((a, b) => {
    const human = humanRank(a) - humanRank(b);
    if (human !== 0) return human;
    if (a.priority !== b.priority) return b.priority - a.priority;
    if (a.since !== b.since) return a.since - b.since;
    const byCase = compareText(a.case_id, b.case_id);
    return byCase !== 0 ? byCase : compareText(a.id, b.id);
  });
}

function pluralize(count: number, noun: string): string {
  return `${noun}${count === 1 ? "" : "s"}`;
}

/**
 * One-line capacity summary for the strip's accessible description.
 *
 * Missing counters read as zero (`parseCapacity`), so the line never invents a
 * number; the three parts are exactly the three facts the strip shows.
 */
export function formatCapacity(capacity: Capacity): string {
  const busy = `${capacity.worker_slots_busy}/${capacity.worker_slots_total} worker ${pluralize(
    capacity.worker_slots_total,
    "slot",
  )} busy`;
  const reserved = `${capacity.worker_slots_reserved_human} ${pluralize(
    capacity.worker_slots_reserved_human,
    "slot",
  )} reserved for human decisions`;
  const saved = `${capacity.slots_saved_by_quiet_waiting} ${pluralize(
    capacity.slots_saved_by_quiet_waiting,
    "slot",
  )} saved by quiet waiting`;
  return `${busy} · ${reserved} · ${saved}`;
}

/**
 * Age of an item from its `since` epoch milliseconds.
 *
 * `0` is the wire's "timestamp unusable" sentinel (`attention/model.py` coerces
 * an unparseable value to the epoch), so it renders as "age unknown" instead of
 * a 20,000-day claim.
 */
export function formatAge(since: number, now: number = Date.now()): string {
  if (!Number.isFinite(since) || since <= 0) return "age unknown";
  const elapsed = Math.max(0, now - since);
  const minutes = Math.floor(elapsed / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/**
 * Read the worklist: `GET /api/attention?include_resolved=false&limit=N`.
 *
 * The reply carries both the items and the capacity snapshot, so one request
 * renders the whole panel.
 */
export async function fetchAttention(
  limit: number = ATTENTION_PAGE_LIMIT,
): Promise<ApiResult<AttentionSnapshot>> {
  const page = Math.max(0, Math.trunc(limit));
  const result = await requestJson(
    `/api/attention?include_resolved=false&limit=${page}`,
  );
  if (!result.ok) return result;
  const payload = asRecord(result.data);
  if (payload === null) return { ok: false, status: 502, error: "invalid_payload" };
  return {
    ok: true,
    data: {
      items: parseAttentionItems(payload.items),
      capacity: parseCapacity(payload.capacity),
    },
  };
}

/**
 * Record a human decision: `POST /api/attention/{id}/resolve`.
 *
 * Idempotent by contract — the gateway returns the original resolution for a
 * repeated call, so a double submit is harmless.
 */
export async function resolveAttention(
  id: string,
  request: ResolveRequest,
): Promise<ApiResult<AttentionItem>> {
  const body: Record<string, string> = { resolution_kind: request.resolution_kind };
  if (request.by) body.by = request.by;
  if (request.detail) body.detail = request.detail;
  const result = await requestJson(
    `/api/attention/${encodeURIComponent(id)}/resolve`,
    { method: "POST", body },
  );
  if (!result.ok) return result;
  const payload = asRecord(result.data);
  const item = payload === null ? null : parseAttentionItem(payload.item);
  if (item === null) return { ok: false, status: 502, error: "invalid_payload" };
  return { ok: true, data: item };
}
