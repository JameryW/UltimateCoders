/**
 * Workflow execution replay data for the product home page.
 *
 * The dashboard aggregates ``subtask_progress`` events into runs on the Python
 * side (``/dashboard/api/workflows/replays``). This module only normalises the
 * wire payload and owns the small presentation helpers the player uses, so the
 * page never has to know whether an event came from durable NATS history or the
 * in-memory fallback.
 */

export type WorkflowReplayStatus = "completed" | "failed" | "running" | "unknown";

/** A replay read that the Dashboard API rejected (usually a missing token). */
export class WorkflowReplayRequestError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`Replay request failed (${status})`);
    this.name = "WorkflowReplayRequestError";
    this.status = status;
  }
}

export interface WorkflowReplayStep {
  index: number;
  agent: string;
  status: string;
  summary: string;
  retryAttempt: number | null;
  parallelGroup: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
}

export interface WorkflowReplayRun {
  id: string;
  taskId: string;
  subtaskId: string;
  status: WorkflowReplayStatus;
  stepTotal: number;
  completedSteps: number;
  percent: number | null;
  startedAt: string | null;
  updatedAt: string | null;
  steps: WorkflowReplayStep[];
}

const STATUSES = new Set<WorkflowReplayStatus>([
  "completed",
  "failed",
  "running",
  "unknown",
]);

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function isoOrNull(value: unknown): string | null {
  const parsed = text(value).trim();
  return parsed === "" ? null : parsed;
}

export function normaliseReplayStatus(value: unknown): WorkflowReplayStatus {
  const status = text(value).trim().toLowerCase();
  return STATUSES.has(status as WorkflowReplayStatus)
    ? (status as WorkflowReplayStatus)
    : "unknown";
}

function normaliseStep(value: unknown, fallbackIndex: number): WorkflowReplayStep | null {
  const raw = record(value);
  const parsedIndex = numberOrNull(raw.index);
  if (raw.index !== undefined && parsedIndex === null) return null;
  const index = parsedIndex ?? fallbackIndex;
  if (index < 0) return null;
  return {
    index,
    agent: text(raw.agent),
    status: text(raw.status).trim() || "unknown",
    summary: text(raw.summary).slice(0, 400),
    retryAttempt: numberOrNull(raw.retry_attempt ?? raw.retryAttempt),
    parallelGroup:
      typeof raw.parallel_group === "string"
        ? raw.parallel_group
        : typeof raw.parallelGroup === "string"
          ? raw.parallelGroup
          : null,
    startedAt: isoOrNull(raw.started_at ?? raw.startedAt),
    finishedAt: isoOrNull(raw.finished_at ?? raw.finishedAt),
    durationMs: numberOrNull(raw.duration_ms ?? raw.durationMs),
  };
}

function normaliseRun(value: unknown, fallbackIndex: number): WorkflowReplayRun | null {
  const raw = record(value);
  const taskId = text(raw.task_id ?? raw.taskId);
  const subtaskId = text(raw.subtask_id ?? raw.subtaskId);
  const rawSteps = Array.isArray(raw.steps) ? raw.steps : [];
  const steps = rawSteps
    .map((step, index) => normaliseStep(step, index))
    .filter((step): step is WorkflowReplayStep => step !== null)
    .sort((left, right) => left.index - right.index);
  if (steps.length === 0) return null;

  const fallbackId = `${taskId || "task"}:${subtaskId || "subtask"}`;
  const stepTotal = numberOrNull(raw.step_total ?? raw.stepTotal);
  const completed = numberOrNull(raw.completed_steps ?? raw.completedSteps);
  const percent = numberOrNull(raw.percent);
  return {
    id: text(raw.id) || `${fallbackId}:${fallbackIndex}`,
    taskId,
    subtaskId,
    status: normaliseReplayStatus(raw.status),
    stepTotal: stepTotal !== null && stepTotal >= steps.length ? stepTotal : steps.length,
    completedSteps:
      completed !== null
        ? completed
        : steps.filter((step) => step.status === "completed" || step.status === "skipped")
            .length,
    percent: percent !== null && percent >= 0 && percent <= 100 ? percent : null,
    startedAt: isoOrNull(raw.started_at ?? raw.startedAt),
    updatedAt: isoOrNull(raw.updated_at ?? raw.updatedAt),
    steps,
  };
}

/** Parse the replay endpoint body into runs, dropping malformed entries. */
export function normaliseReplayPayload(payload: unknown): WorkflowReplayRun[] {
  const raw = record(payload);
  const runs = Array.isArray(raw.runs) ? raw.runs : [];
  return runs
    .map((run, index) => normaliseRun(run, index))
    .filter((run): run is WorkflowReplayRun => run !== null);
}

/**
 * Fetch recent workflow runs. A dashboard on a static host may answer with 404
 * HTML; the caller treats any non-OK or malformed answer as "no replay data"
 * instead of tearing down the page.
 */
export async function fetchWorkflowReplays(
  fetchImpl: typeof fetch,
  token: string | null = null,
  signal?: AbortSignal,
): Promise<WorkflowReplayRun[]> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetchImpl("/dashboard/api/workflows/replays", {
    headers,
    signal,
  });
  if (!response.ok) {
    throw new WorkflowReplayRequestError(response.status);
  }
  const payload = await response.json();
  if (payload === null || typeof payload !== "object" || !Array.isArray(record(payload).runs)) {
    throw new Error("Replay response is malformed");
  }
  return normaliseReplayPayload(payload);
}

/**
 * Step summaries arrive from the agent harness verbatim: newlines, terminal
 * control bytes and a leading "done" marker are common. They are presentation
 * noise in a one-line replay row, so collapse them here instead of teaching
 * every render site the same cleanup.
 */
export function cleanReplaySummary(value: string | null | undefined): string {
  const collapsed = (value ?? "")
    .replace(/\uFFFD/g, " ")
    // Control bytes become spaces; the whitespace pass below collapses them.
    .replace(/\p{Cc}/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!collapsed) return "";

  // Strip the harness's leading completion marker ("done", "done ✓"), but leave
  // words such as "Donegal" untouched: the marker must end at a boundary.
  const withoutMarker = collapsed
    .replace(/^[\s✓✔•·]+/, "")
    .replace(/^(?:done|completed|finished|完成)(?=$|[\s.!,:;—–-])[\s✓✔•·.!,:;—–-]*/i, "")
    .replace(/^[\s✓✔•·]+/, "");
  return withoutMarker.trim();
}

/** Human-friendly wall-clock duration; null means the run is not settled yet. */
export function formatReplayDuration(durationMs: number | null): string {
  if (durationMs === null || !Number.isFinite(durationMs) || durationMs < 0) {
    return "—";
  }
  if (durationMs < 1_000) return `${Math.round(durationMs)}ms`;
  if (durationMs < 60_000) return `${(durationMs / 1_000).toFixed(1)}s`;
  const minutes = Math.floor(durationMs / 60_000);
  const seconds = Math.round((durationMs % 60_000) / 1_000);
  return `${minutes}m ${seconds.toString().padStart(2, "0")}s`;
}
