/**
 * Failure-mode heatmap for the v10 process-aware dashboard.
 *
 * The v10 acceptance pipeline records a bounded stream of failure events. Each
 * event names a failure mode from the fixed catalogue below; the heatmap turns
 * that stream into a phase × mode count matrix and a deterministic-event share.
 * Unknown modes are dropped rather than invented, counts are bounded by the
 * caller's window, and rates are derived numbers (never supplied on the wire).
 *
 * `classifyPtaQuality` is the presentation-side counterpart of the PTA quality
 * tiers: a passing trajectory is only `ideal` when the process was clean and
 * backed by test evidence; an explicitly lucky pass, a pass without test
 * evidence, or a blind-retry pattern is `lucky`; everything else is `solid`.
 */

export const FAILURE_PHASES = [
  "planning",
  "retrieval",
  "editing",
  "tooling",
  "testing",
  "review",
  "delivery",
] as const;

export type FailurePhase = (typeof FAILURE_PHASES)[number];

export type FailureSeverity = 1 | 2 | 3 | 4 | 5;

/** One entry in the fixed failure-mode catalogue. */
export interface FailureMode {
  id: string;
  label: string;
  phase: FailurePhase;
  /** Relative severity weight, 1 (minor) through 5 (critical). */
  severity: FailureSeverity;
  /** Deterministic modes are reproducible signals, not sampled judgements. */
  deterministic: boolean;
}

/**
 * The 24 v10 failure modes. Exactly 18 are deterministic; the six
 * non-deterministic modes are judgement/sampling signals and are excluded from
 * the deterministic share calculated by `buildFailureHeatmap`.
 */
export const FAILURE_MODES = [
  {
    id: "plan_missing_step",
    label: "Plan omits a required step",
    phase: "planning",
    severity: 4,
    deterministic: false,
  },
  {
    id: "plan_scope_drift",
    label: "Plan drifts beyond task scope",
    phase: "planning",
    severity: 3,
    deterministic: false,
  },
  {
    id: "plan_overdecomposition",
    label: "Plan splits into excessive micro-steps",
    phase: "planning",
    severity: 2,
    deterministic: false,
  },
  {
    id: "retrieval_missing_context",
    label: "Required context never retrieved",
    phase: "retrieval",
    severity: 5,
    deterministic: true,
  },
  {
    id: "retrieval_stale_snapshot",
    label: "Retrieved an outdated repository snapshot",
    phase: "retrieval",
    severity: 4,
    deterministic: true,
  },
  {
    id: "retrieval_irrelevant",
    label: "Retrieved content irrelevant to the task",
    phase: "retrieval",
    severity: 2,
    deterministic: false,
  },
  {
    id: "edit_wrong_file",
    label: "Edit landed in the wrong file",
    phase: "editing",
    severity: 5,
    deterministic: true,
  },
  {
    id: "edit_partial_patch",
    label: "Patch implements only part of the change",
    phase: "editing",
    severity: 4,
    deterministic: true,
  },
  {
    id: "edit_regression",
    label: "Edit introduces a regression",
    phase: "editing",
    severity: 5,
    deterministic: true,
  },
  {
    id: "edit_unrelated_change",
    label: "Edit includes unrelated changes",
    phase: "editing",
    severity: 3,
    deterministic: true,
  },
  {
    id: "tool_command_failed",
    label: "Tool command failed",
    phase: "tooling",
    severity: 3,
    deterministic: true,
  },
  {
    id: "tool_timeout",
    label: "Tool invocation timed out",
    phase: "tooling",
    severity: 3,
    deterministic: true,
  },
  {
    id: "tool_invalid_arguments",
    label: "Tool received invalid arguments",
    phase: "tooling",
    severity: 2,
    deterministic: true,
  },
  {
    id: "tool_permission_denied",
    label: "Tool was denied required permission",
    phase: "tooling",
    severity: 4,
    deterministic: true,
  },
  {
    id: "test_not_run",
    label: "Tests were never run",
    phase: "testing",
    severity: 5,
    deterministic: true,
  },
  {
    id: "test_failure_ignored",
    label: "Failing test was ignored",
    phase: "testing",
    severity: 5,
    deterministic: true,
  },
  {
    id: "test_insufficient_coverage",
    label: "Tests cover too little of the change",
    phase: "testing",
    severity: 4,
    deterministic: true,
  },
  {
    id: "test_flaky_outcome",
    label: "Test outcome was flaky",
    phase: "testing",
    severity: 2,
    deterministic: false,
  },
  {
    id: "review_missed_regression",
    label: "Review missed a known regression",
    phase: "review",
    severity: 5,
    deterministic: true,
  },
  {
    id: "review_scope_mismatch",
    label: "Review checked the wrong scope",
    phase: "review",
    severity: 3,
    deterministic: true,
  },
  {
    id: "review_false_approve",
    label: "Review approved an unverified change",
    phase: "review",
    severity: 4,
    deterministic: false,
  },
  {
    id: "delivery_uncommitted_change",
    label: "Delivered with uncommitted changes",
    phase: "delivery",
    severity: 4,
    deterministic: true,
  },
  {
    id: "delivery_broken_artifact",
    label: "Delivered a broken artifact",
    phase: "delivery",
    severity: 5,
    deterministic: true,
  },
  {
    id: "delivery_missing_summary",
    label: "Delivered without a change summary",
    phase: "delivery",
    severity: 2,
    deterministic: true,
  },
] as const satisfies readonly FailureMode[];

/** One phase × mode bucket. `rate` and `deterministic` are derived per cell. */
export interface FailureHeatmapCell {
  phase: FailurePhase;
  modeId: string;
  count: number;
  /** Share of all counted events, rounded to four decimal places (0–1). */
  rate: number;
  deterministic: boolean;
}

/** The complete, render-ready heatmap payload. */
export interface FailureHeatmap {
  phases: FailurePhase[];
  modes: FailureMode[];
  cells: FailureHeatmapCell[];
  totalEvents: number;
  /** Share of counted events whose mode is deterministic, rounded to 0–1. */
  deterministicRate: number;
}

/** A raw failure event as emitted by the acceptance pipeline. */
export interface FailureEvent {
  type?: string;
  phase?: string;
  mode?: string;
  details?: Record<string, unknown>;
}

export interface FailureHeatmapOptions {
  /** Maximum number of input events inspected. Defaults to 20,000. */
  maxEvents?: number;
  /** Maximum count attributed to one cell. Defaults to 20,000. */
  maxCount?: number;
}

const DEFAULT_MAX_EVENTS = 20_000;
const DEFAULT_MAX_COUNT = 20_000;
const MAX_BOUND = 1_000_000;

const PHASE_SET = new Set<string>(FAILURE_PHASES);
const MODE_BY_ID = new Map<string, FailureMode>(
  FAILURE_MODES.map((mode) => [mode.id, mode as FailureMode]),
);

function boundedInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  return Math.min(MAX_BOUND, Math.max(minimum, Math.floor(value)));
}

function ratio(count: number, total: number): number {
  if (total <= 0) return 0;
  return Math.round((count / total) * 10_000) / 10_000;
}

function eventRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/**
 * Aggregate raw failure events into a bounded heatmap.
 *
 * Counting rules:
 * - only modes in `FAILURE_MODES` count; unknown or missing modes are ignored;
 * - at most `maxEvents` input records are inspected;
 * - one cell may not exceed `maxCount`; events above the cap are not counted in
 *   `totalEvents` either, so every rate remains a share of the counted set;
 * - the mode catalogue owns the canonical phase for each cell.
 */
export function buildFailureHeatmap(
  events: FailureEvent[],
  options: FailureHeatmapOptions = {},
): FailureHeatmap {
  const maxEvents = boundedInteger(options.maxEvents, DEFAULT_MAX_EVENTS, 0);
  const maxCount = boundedInteger(options.maxCount, DEFAULT_MAX_COUNT, 1);
  const input = Array.isArray(events) ? events : [];
  const counts = new Map<string, number>();
  for (const mode of FAILURE_MODES) counts.set(mode.id, 0);

  let inspected = 0;
  let totalEvents = 0;
  let deterministicEvents = 0;

  for (const event of input) {
    if (inspected >= maxEvents) break;
    inspected += 1;
    const raw = eventRecord(event);
    if (raw === null) continue;
    const modeId = typeof raw.mode === "string" ? raw.mode.trim().toLowerCase() : "";
    const mode = MODE_BY_ID.get(modeId);
    if (mode === undefined) continue;
    const current = counts.get(mode.id) ?? 0;
    if (current >= maxCount) continue;
    counts.set(mode.id, current + 1);
    totalEvents += 1;
    if (mode.deterministic) deterministicEvents += 1;
  }

  const cells: FailureHeatmapCell[] = FAILURE_MODES.map((mode) => {
    const count = counts.get(mode.id) ?? 0;
    return {
      phase: mode.phase,
      modeId: mode.id,
      count,
      rate: ratio(count, totalEvents),
      deterministic: mode.deterministic,
    };
  });

  return {
    phases: [...FAILURE_PHASES],
    modes: FAILURE_MODES.map((mode) => ({ ...mode })),
    cells,
    totalEvents,
    deterministicRate: ratio(deterministicEvents, totalEvents),
  };
}

export interface PtaQualityInput {
  /** The trajectory was explicitly flagged as a lucky pass. */
  luckyPass?: boolean;
  /** Test evidence backs the final outcome. */
  testEvidence?: boolean;
  /** The trajectory followed a clean exploration→edit→verify order. */
  processClean?: boolean;
  /** Number of blind retries before the passing attempt. */
  retries?: number;
}

export type PtaQuality = "ideal" | "solid" | "lucky";

/**
 * Classify a passing PTA trajectory into the v10 quality tiers.
 *
 * - `lucky`: an explicit lucky-pass flag, no test evidence, or a dirty process
 *   with at least two blind retries;
 * - `ideal`: test evidence, a clean process, and no retries;
 * - `solid`: every other passing trajectory.
 */
export function classifyPtaQuality(input: PtaQualityInput = {}): PtaQuality {
  const retries =
    typeof input.retries === "number" && Number.isFinite(input.retries)
      ? Math.max(0, Math.floor(input.retries))
      : 0;

  if (input.luckyPass === true) return "lucky";
  if (input.testEvidence === false) return "lucky";
  if (input.processClean === false && retries >= 2) return "lucky";
  if (
    input.testEvidence === true &&
    input.processClean === true &&
    retries === 0
  ) {
    return "ideal";
  }
  return "solid";
}

/** Exposed for callers that want to validate a raw phase string. */
export function isFailurePhase(value: unknown): value is FailurePhase {
  return typeof value === "string" && PHASE_SET.has(value);
}
