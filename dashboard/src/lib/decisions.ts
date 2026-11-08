/**
 * Decision Record trail helpers.
 *
 * The runtime records one attempt-scoped audit join under the key
 * `task:subtask:attempt` (python/ultimate_coders/decision_record.py): the route
 * choice, a pointer at the context manifest, the terminal verdict, and -- since
 * V8 P7 -- the attempt's local effects as `terminal.workspace`. The record
 * carries no prompt content, no diffs and no tool output, so every formatter
 * here reads known fields and never an arbitrary blob.
 *
 * Ordering belongs to the endpoint. The decisions endpoint returns its key
 * window as-is (subtask-lexicographic, attempt-numeric); this module renders
 * what it is given rather than inventing a second order.
 */

export interface WorkspaceEvidenceFile {
  path?: string;
  change_type?: string;
}

/** `terminal.workspace` as P7 writes it (paths + change types only). */
export interface WorkspaceEvidence {
  files?: WorkspaceEvidenceFile[];
  file_count?: number;
  tool_calls?: string[];
  truncated?: boolean;
}

export interface DecisionTerminal {
  success?: boolean;
  summary?: string;
  error_class?: string;
  workspace?: WorkspaceEvidence;
}

export interface DecisionRecord {
  key: string;
  worker_id?: string;
  route?: Record<string, unknown>;
  context_digest?: string;
  context_key?: string;
  terminal?: DecisionTerminal;
  recorded_at?: number;
  terminal_at?: number;
}

/** One endpoint response (`GET /dashboard/api/decisions`). */
export interface DecisionPagePayload {
  decisions?: DecisionRecord[];
  count?: number;
  total?: number;
  /** Cursor for the next page; null/absent means the page was short. */
  next_cursor?: string | null;
}

/** The trail as the panel holds it: loaded rows plus the paging cursor. */
export interface DecisionTrailState {
  records: DecisionRecord[];
  total: number;
  /** Cursor for the next page; undefined means the trail is exhausted. */
  nextCursor?: string;
}

export interface DecisionKeyParts {
  taskId: string;
  subtaskId: string;
  attempt: number;
}

/**
 * Split `task:subtask:attempt` into (prefix, numeric attempt). The backend's
 * task scan sorts by exactly this key; a non-numeric tail is attempt 0 (the
 * backend's `int(attempt) if attempt.isdigit() else 0`).
 */
export function decisionOrderKey(key: string): [string, number] {
  const at = key.lastIndexOf(":");
  const prefix = at < 0 ? key : key.slice(0, at);
  const tail = at < 0 ? "" : key.slice(at + 1);
  return [prefix, /^\d+$/.test(tail) ? Number(tail) : 0];
}

/** Split on the last two colons so a task id may itself contain one. */
export function parseDecisionKey(key: string): DecisionKeyParts {
  const last = key.lastIndexOf(":");
  const second = last < 0 ? -1 : key.lastIndexOf(":", last - 1);
  if (last < 0) return { taskId: key, subtaskId: "", attempt: 0 };
  if (second < 0) {
    return { taskId: "", subtaskId: key.slice(0, last), attempt: toAttempt(key.slice(last + 1)) };
  }
  return {
    taskId: key.slice(0, second),
    subtaskId: key.slice(second + 1, last),
    attempt: toAttempt(key.slice(last + 1)),
  };
}

function toAttempt(tail: string): number {
  return /^\d+$/.test(tail) ? Number(tail) : 0;
}

/**
 * The trail request: one task, the `order=recent` time window (newest first)
 * bounded by `limit`, optionally resuming after `before` (the previous page's
 * `next_cursor`). Key order is a single oldest-key window with no cursor, so
 * asking for it would hide exactly the attempts an audit is about; ids are
 * encoded because a task id may contain characters a raw query string splits on.
 */
export function decisionTrailQuery(
  taskId: string,
  options: {
    limit?: number;
    order?: "key" | "recent";
    before?: string | null;
    subtaskId?: string | null;
  } = {},
): string {
  const limit = options.limit ?? 100;
  const order = options.order ?? "recent";
  const parts = [
    `task_id=${encodeURIComponent(taskId)}`,
    `limit=${limit}`,
    `order=${order}`,
  ];
  if (options.subtaskId) parts.push(`subtask_id=${encodeURIComponent(options.subtaskId)}`);
  if (options.before) parts.push(`before=${encodeURIComponent(options.before)}`);
  return `/dashboard/api/decisions?${parts.join("&")}`;
}

/** `subtask s1 · ` when scoped; empty for the task-wide trail. */
export function decisionScopeLabel(subtaskId?: string | null): string {
  return subtaskId ? `subtask ${subtaskId} · ` : "";
}

/**
 * Merge one page into the trail: rows are deduped by key (a resumed page must
 * never repeat a row), `total` follows the endpoint, and the cursor becomes
 * undefined when the page was short — the end, not a stale anchor.
 */
export function appendDecisionPage(
  trail: DecisionTrailState,
  payload: DecisionPagePayload,
): DecisionTrailState {
  const page = decisionsFromPayload(payload);
  const seen = new Set(trail.records.map((record) => record.key));
  const appended = page.records.filter((record) => !seen.has(record.key));
  return {
    records: [...trail.records, ...appended],
    total: page.total,
    nextCursor: payload?.next_cursor ?? undefined,
  };
}

/** Terminal time in UTC, deterministic on every machine. */
export function decisionRecordedAt(epoch?: number): string {
  if (typeof epoch !== "number" || !Number.isFinite(epoch) || epoch < 0) return "";
  const at = new Date(epoch * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())}:${pad(at.getUTCSeconds())}Z`;
}

/** `succeeded` / `failed (<error_class>)` — never a guessed verdict. */
export function decisionHeadline(record: DecisionRecord): string {
  const terminal = record.terminal;
  if (!terminal || typeof terminal.success !== "boolean") return "no verdict";
  if (terminal.success) return "succeeded";
  const errorClass = terminal.error_class;
  return errorClass ? `failed (${errorClass})` : "failed";
}

/** `implementation · medium · gpt-x` for the route the attempt actually took. */
export function decisionRouteLine(route?: Record<string, unknown>): string {
  if (!route) return "";
  const parts = ["stage", "risk", "model"]
    .map((field) => route[field])
    .filter((value): value is string => typeof value === "string" && value.trim() !== "");
  return parts.join(" · ");
}

/** Short context pointer: the digest's first 12 hex chars, never the summary. */
export function decisionDigestLabel(digest?: string): string {
  if (!digest) return "";
  return `ctx ${digest.slice(0, 12)}`;
}

/**
 * One line for the attempt's local effects, or "" when none were recorded.
 * Paths are shown up to `pathLimit`, then `+N`; the tool list is capped by the
 * record itself (P7 caps at 20) and is not re-sliced here.
 */
export function workspaceLine(workspace?: WorkspaceEvidence, pathLimit = 3): string {
  if (!workspace) return "";
  const files = Array.isArray(workspace.files) ? workspace.files : [];
  const paths = files
    .map((file) => file.path)
    .filter((path): path is string => typeof path === "string" && path !== "");
  const count =
    typeof workspace.file_count === "number" && Number.isFinite(workspace.file_count)
      ? workspace.file_count
      : paths.length;
  const tools = Array.isArray(workspace.tool_calls)
    ? workspace.tool_calls.filter((tool): tool is string => typeof tool === "string" && tool !== "")
    : [];

  const shown = paths.slice(0, pathLimit);
  const hidden = Math.max(0, paths.length - shown.length);
  const filePart = `${count} file${count === 1 ? "" : "s"}`;
  const pathPart = shown.length
    ? ` (${shown.join(", ")}${hidden ? `, +${hidden}` : ""})`
    : "";
  const toolPart = tools.length ? ` · tools: ${tools.join(", ")}` : "";
  const truncated = workspace.truncated === true ? " · truncated" : "";
  return `${filePart}${pathPart}${toolPart}${truncated}`;
}

/** Rows plus the endpoint's counts, without inventing a total. */
export function decisionsFromPayload(payload: DecisionPagePayload): {
  records: DecisionRecord[];
  count: number;
  total: number;
} {
  const records = Array.isArray(payload?.decisions) ? payload.decisions : [];
  const count =
    typeof payload?.count === "number" && Number.isFinite(payload.count)
      ? payload.count
      : records.length;
  const total =
    typeof payload?.total === "number" && Number.isFinite(payload.total)
      ? payload.total
      : count;
  return { records, count, total };
}

/** `3 of 12 attempts` — the window is the scan only when they agree. */
export function decisionCountLine(count: number, total: number): string {
  if (total <= count) return `${count} attempt${count === 1 ? "" : "s"}`;
  return `${count} of ${total} attempts`;
}
