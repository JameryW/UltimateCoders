import type { SubtaskSummary, TaskSummary } from "@/types/dashboard";

const DONE_STATUSES = new Set(["completed", "done", "success", "succeeded"]);
const RUNNING_STATUSES = new Set(["in_progress", "running", "assigned", "started", "executing"]);

export function clampPercent(value: number | null | undefined): number {
  const n = Number(value ?? 0);
  return Math.max(0, Math.min(100, Math.round(Number.isFinite(n) ? n : 0)));
}

export function isSubtaskDone(subtask: Pick<SubtaskSummary, "status" | "step_status" | "percent">): boolean {
  return DONE_STATUSES.has(subtask.status)
    || subtask.step_status === "completed"
    || clampPercent(subtask.percent) >= 100;
}

export function isSubtaskRunning(subtask: Pick<SubtaskSummary, "status" | "step_status" | "percent">): boolean {
  if (isSubtaskDone(subtask)) return false;
  return RUNNING_STATUSES.has(subtask.status)
    || subtask.step_status === "started"
    || (typeof subtask.percent === "number" && clampPercent(subtask.percent) > 0 && clampPercent(subtask.percent) < 100);
}

export type AggregateSubtaskProgress = {
  completed: number;
  total: number;
  /** Mean of subtask percents when any report percent; else completed/total * 100. */
  percent: number;
  /** First running subtask phase (or empty). */
  phase: string;
  /** First running subtask step_agent (or empty). */
  stepAgent: string;
  hasRunning: boolean;
};

/** Aggregate selected-task subtask progress for the history live strip / list chip. */
export function aggregateSubtaskProgress(
  subtasks: SubtaskSummary[] | null | undefined,
): AggregateSubtaskProgress {
  const list = subtasks ?? [];
  const total = list.length;
  const completed = list.filter(isSubtaskDone).length;
  const running = list.find(isSubtaskRunning);
  const withPercent = list.filter((s) => typeof s.percent === "number");
  const meanPercent = withPercent.length > 0
    ? withPercent.reduce((sum, s) => sum + clampPercent(s.percent), 0) / withPercent.length
    : total > 0 ? (completed / total) * 100 : 0;
  return {
    completed,
    total,
    percent: clampPercent(meanPercent),
    phase: running?.phase ?? "",
    stepAgent: running?.step_agent ?? "",
    hasRunning: Boolean(running),
  };
}

/** Compact chip for in_progress list rows — percent + first running phase. */
export function taskListProgressChip(
  task: Pick<TaskSummary, "status" | "subtasks">,
): { percent: number; phase: string } | null {
  if (task.status !== "in_progress" && task.status !== "running") return null;
  const agg = aggregateSubtaskProgress(task.subtasks);
  if (agg.total === 0 && !agg.hasRunning) return null;
  return { percent: agg.percent, phase: agg.phase };
}

/** Map TaskEvent.type prefix → command-center session role. */
export type SessionEventRole = "lead" | "swarm" | "dispatch" | "advisor";

export function sessionRoleForEventType(type: string): SessionEventRole {
  if (type.startsWith("task_")) return "lead";
  if (type.startsWith("subtask_")) return "swarm";
  if (type.startsWith("tool_")) return "dispatch";
  if (type.startsWith("llm_")) return "advisor";
  return "dispatch";
}
