/**
 * Governance approval ("who is allowed to run this human-tier tool") HTTP client.
 *
 * The dashboard surface is served by
 * `python/ultimate_coders/dashboard/approval_api.py`, mounted without a prefix:
 *
 *   GET  /api/approvals                    → `{ approvals: […] }`
 *   POST /api/approvals                    → `{ approval: {…} }`  (ask for a permit)
 *   POST /api/approvals/{id}/decide        → `{ approval: {…} }`
 *
 * The gateway (Rust `ApprovalStore`) owns the permit; this module only reads the
 * queue and forwards a human decision. Wire facts pinned here, because the panel
 * must not guess them:
 *
 * 1. `requested_at` / `expires_at` / `decided_at` / `used_at` arrive as RFC 3339
 *    strings and are normalised to epoch milliseconds (`0` = unusable).
 * 2. The queue order is deterministic: **pending first**, then the soonest
 *    `expires_at`, then `id`. A pending decision that expires in two minutes is
 *    more urgent than one that expires tonight, so expiry is the tiebreaker
 *    instead of the request age.
 * 3. `503 {"error": "governance_unavailable"}` means the bridge is down — an
 *    operational state, not a failed request. Every helper *resolves* to a
 *    discriminated result so a dead bridge cannot turn the poll loop into a
 *    toast storm (same rule as `attentionApi`).
 *
 * Junk records are filtered, never thrown: one unreadable approval must not hide
 * the rest of the queue from the human deciding them.
 *
 * Honest limitation: the decision is always sent as a `user` principal. The
 * dashboard cannot act as a service or an agent, because authority must not be
 * borrowed from the UI — and the gateway would refuse it anyway. *Requesting* is
 * the mirror image: the requester must be an `agent` or a `service` (a human does
 * not ask themselves for permission), and the gateway records who asked so the
 * same principal can never decide it.
 */

import {
  epochMillis,
  asRecord,
  optionalText,
  requestJson,
  type ApiResult,
} from "./attentionApi.ts";

/** Wire tokens for an approval's state (mirrors Rust `ApprovalState`). */
export const APPROVAL_STATES = [
  "pending",
  "approved",
  "rejected",
  "expired",
  "used",
] as const;

export type ApprovalState = (typeof APPROVAL_STATES)[number];

/** Default page size for the queue (the endpoint's own default is 50). */
export const APPROVAL_PAGE_LIMIT = 50;

/** One approval, normalised for rendering. */
export interface Approval {
  id: string;
  tool: string;
  case_id?: string;
  project_id?: string;
  /** Opaque digest of the requested action; displayed, never interpreted. */
  action_digest?: string;
  requested_by_kind: string;
  requested_by_id: string;
  reason?: string;
  state: ApprovalState;
  /** Epoch milliseconds. */
  requested_at: number;
  /** Epoch milliseconds; the promise lapses here. */
  expires_at: number;
  decided_at?: number;
  decided_by_id?: string;
  note?: string;
  used_at?: number;
}

function parseState(value: unknown): ApprovalState | null {
  if (typeof value !== "string") return null;
  const token = value.trim().toLowerCase();
  return (APPROVAL_STATES as readonly string[]).includes(token)
    ? (token as ApprovalState)
    : null;
}

/** Normalise one wire record; `null` when it cannot be rendered honestly. */
export function parseApproval(value: unknown): Approval | null {
  const record = asRecord(value);
  if (record === null) return null;
  const id = optionalText(record.id);
  const tool = optionalText(record.tool);
  const state = parseState(record.state);
  if (!id || !tool || state === null) return null;
  return {
    id,
    tool,
    case_id: optionalText(record.case_id),
    project_id: optionalText(record.project_id),
    action_digest: optionalText(record.action_digest),
    requested_by_kind: optionalText(record.requested_by_kind) ?? "unknown",
    requested_by_id: optionalText(record.requested_by_id) ?? "unknown",
    reason: optionalText(record.reason),
    state,
    requested_at: epochMillis(record.requested_at),
    expires_at: epochMillis(record.expires_at),
    decided_at: record.decided_at === undefined ? undefined : epochMillis(record.decided_at),
    decided_by_id: optionalText(record.decided_by_id),
    note: optionalText(record.note),
    used_at: record.used_at === undefined ? undefined : epochMillis(record.used_at),
  };
}

/** Normalise a list, dropping records that cannot be rendered. */
export function parseApprovals(value: unknown): Approval[] {
  if (!Array.isArray(value)) return [];
  const approvals: Approval[] = [];
  for (const entry of value) {
    const approval = parseApproval(entry);
    if (approval !== null) approvals.push(approval);
  }
  return approvals;
}

/** Whether this approval still needs a human. */
export function isPending(approval: Approval): boolean {
  return approval.state === "pending";
}

/** Whether the permit can still be spent (approved and not expired). */
export function isUsable(approval: Approval, now: number = Date.now()): boolean {
  return approval.state === "approved" && approval.expires_at > now;
}

/**
 * Deterministic queue order: pending first, then the soonest expiry, then `id`.
 *
 * The input array is never mutated.
 */
export function approvalOrder(approvals: readonly Approval[]): Approval[] {
  const rank = (approval: Approval): number => (isPending(approval) ? 0 : 1);
  return [...approvals].sort((a, b) => {
    const byState = rank(a) - rank(b);
    if (byState !== 0) return byState;
    if (a.expires_at !== b.expires_at) {
      // `0` means "unusable timestamp"; it sorts last rather than first.
      if (a.expires_at === 0) return 1;
      if (b.expires_at === 0) return -1;
      return a.expires_at - b.expires_at;
    }
    if (a.id === b.id) return 0;
    return a.id < b.id ? -1 : 1;
  });
}

function pluralize(count: number, noun: string): string {
  return `${noun}${count === 1 ? "" : "s"}`;
}

/**
 * How long the decision stays meaningful.
 *
 * `0` is the wire's "unusable" sentinel, so it reads as "expiry unknown" instead
 * of a 20,000-day claim (the same honesty rule `formatAge` applies to `since`).
 */
export function formatExpiry(approval: Approval, now: number = Date.now()): string {
  if (approval.state === "used") return "permit already used";
  if (approval.state === "rejected") return "rejected";
  if (approval.state === "expired") return "expired";
  if (approval.state === "approved") {
    const remaining = approval.expires_at - now;
    if (approval.expires_at <= 0) return "expiry unknown";
    if (remaining <= 0) return "permit lapsed";
    return `permit valid for ${formatMinutes(remaining)}`;
  }
  if (approval.expires_at <= 0) return "expiry unknown";
  const remaining = approval.expires_at - now;
  if (remaining <= 0) return "expired, no decision";
  return `expires in ${formatMinutes(remaining)}`;
}

function formatMinutes(milliseconds: number): string {
  const minutes = Math.floor(milliseconds / 60_000);
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `${minutes} ${pluralize(minutes, "minute")}`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `${hours} ${pluralize(hours, "hour")}` : `${hours}h ${rest}m`;
}

/** Read the queue: `GET /api/approvals?state=…&limit=N`. */
export async function fetchApprovals(
  state?: ApprovalState,
  limit: number = APPROVAL_PAGE_LIMIT,
): Promise<ApiResult<Approval[]>> {
  const page = Math.max(0, Math.trunc(limit));
  const query = new URLSearchParams({ limit: String(page) });
  if (state) query.set("state", state);
  const result = await requestJson(`/api/approvals?${query.toString()}`);
  if (!result.ok) return result;
  const payload = asRecord(result.data);
  if (payload === null) return { ok: false, status: 502, error: "invalid_payload" };
  return { ok: true, data: parseApprovals(payload.approvals) };
}

/**
 * Approve or reject: `POST /api/approvals/{id}/decide`.
 *
 * The note is optional but encouraged: it is what a later audit reads. A `401`
 * means the deployment requires `UC_DASHBOARD_TOKEN` and the browser carries no
 * (or a wrong) token — surfaced as-is so the panel can say so instead of looking
 * like a missing item.
 */
export async function decideApproval(
  id: string,
  approve: boolean,
  note?: string,
): Promise<ApiResult<Approval>> {
  const body: Record<string, unknown> = { approve };
  if (note !== undefined && note.trim()) body.note = note.trim();
  const result = await requestJson(`/api/approvals/${encodeURIComponent(id)}/decide`, {
    method: "POST",
    body,
  });
  if (!result.ok) return result;
  const payload = asRecord(result.data);
  const approval = payload === null ? null : parseApproval(payload.approval);
  if (approval === null) return { ok: false, status: 502, error: "invalid_payload" };
  return { ok: true, data: approval };
}

/** Who may ask for a permit. `user` is deliberately impossible here. */
export const REQUESTER_KINDS = ["agent", "service"] as const;
export type RequesterKind = (typeof REQUESTER_KINDS)[number];

/** The human-tier tools the gateway gates; free text stays allowed for others. */
export const HUMAN_TIER_TOOLS = ["scale_workers", "docker_run"] as const;

export interface ApprovalRequestInput {
  tool: string;
  caseId?: string;
  projectId?: string;
  actionDigest?: string;
  reason?: string;
  requestedByKind?: RequesterKind;
  requestedById?: string;
  /** Positive integer; the gateway bounds it by its own maximum. */
  ttlMinutes?: number;
}

/**
 * Ask a human to permit one call to a human-tier tool.
 *
 * Sent as `agent`/`service` only: the wire contract refuses a `user` requester,
 * and the UI must not offer what the gateway would reject.
 */
export async function requestApproval(
  input: ApprovalRequestInput,
): Promise<ApiResult<Approval>> {
  const tool = input.tool.trim();
  if (!tool) return { ok: false, status: 400, error: "invalid_body" };
  const body: Record<string, unknown> = { tool };
  const kind = input.requestedByKind ?? "agent";
  if (!REQUESTER_KINDS.includes(kind)) return { ok: false, status: 400, error: "invalid_body" };
  body.requested_by_kind = kind;
  const id = (input.requestedById ?? "uc-worker").trim() || "uc-worker";
  body.requested_by_id = id;
  for (const [key, value] of [
    ["case_id", input.caseId],
    ["project_id", input.projectId],
    ["action_digest", input.actionDigest],
    ["reason", input.reason],
  ] as const) {
    if (value !== undefined && value.trim()) body[key] = value.trim();
  }
  if (input.ttlMinutes !== undefined) {
    const ttl = Math.trunc(input.ttlMinutes);
    if (!Number.isFinite(ttl) || ttl <= 0) {
      return { ok: false, status: 400, error: "invalid_body" };
    }
    body.ttl_minutes = ttl;
  }
  const result = await requestJson("/api/approvals", { method: "POST", body });
  if (!result.ok) return result;
  const payload = asRecord(result.data);
  const approval = payload === null ? null : parseApproval(payload.approval);
  if (approval === null) return { ok: false, status: 502, error: "invalid_payload" };
  return { ok: true, data: approval };
}
