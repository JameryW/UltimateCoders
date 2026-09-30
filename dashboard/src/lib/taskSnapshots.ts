import type { DashboardEvent, TaskSummary, TasksData } from "../types/dashboard.ts";
import { normalizeGrpcStatus } from "./grpcStatus.ts";
import { taskTimestampToISO } from "./grpcTimestamp.ts";

export function ignoresLateStart(status: string, eventType: string): boolean {
  return ["completed", "failed", "cancelled"].includes(status)
    && ["subtask_assigned", "subtask_started"].includes(eventType);
}

export function terminalEventStatus(eventType: string, currentStatus?: string): "failed" | "cancelled" {
  return eventType === "task_cancelled" || currentStatus === "cancelled" ? "cancelled" : "failed";
}

/** The legacy task snapshot uses Failed for cancellation; history retains its cause. */
export function restoreCancelledTasks(data: TasksData, events: DashboardEvent[]): TasksData {
  const cancelled = new Set(events.filter((event) => event.type === "task_cancelled")
    .map((event) => event.details.task_id).filter((id): id is string => typeof id === "string"));
  const tasks = data.tasks.map((task) => cancelled.has(task.id) ? { ...task, status: "cancelled" } : task);
  const status_counts: Record<string, number> = {};
  for (const task of tasks) status_counts[task.status] = (status_counts[task.status] ?? 0) + 1;
  return { ...data, tasks, status_counts, pending_task_count: status_counts.pending ?? status_counts.submitted ?? 0 };
}

/** Explicit fresh retry reopens a failed task; pause/cancellation stay authoritative. */
export function retryParentState(task: TaskSummary, timestamp: string): Pick<TaskSummary, "status" | "updated_at"> {
  if (["paused", "cancelled"].includes(task.status)
    || Date.parse(timestamp) < Date.parse(task.updated_at)) {
    return { status: task.status, updated_at: task.updated_at };
  }
  return { status: "in_progress", updated_at: timestamp };
}

type WireTask = Omit<TaskSummary, "created_at" | "updated_at"> & {
  created_at: string | number;
  updated_at: string | number;
};

/** SSE snapshots carry numeric Unix seconds; gRPC conversion already yields ISO. */
export function normalizeTaskSnapshot(data: Omit<TasksData, "tasks"> & { tasks: WireTask[] }): TasksData {
  const iso = (value: string | number) => typeof value === "number"
    ? taskTimestampToISO(BigInt(value)) : value;
  const tasks = data.tasks.map((task) => ({
    ...task,
    status: normalizeGrpcStatus(task.status),
    subtasks: task.subtasks?.map((subtask) => ({ ...subtask, status: normalizeGrpcStatus(subtask.status) })),
    created_at: iso(task.created_at),
    updated_at: iso(task.updated_at),
  }));
  const status_counts: Record<string, number> = {};
  for (const task of tasks) status_counts[task.status] = (status_counts[task.status] ?? 0) + 1;
  return { ...data, tasks, status_counts, pending_task_count: status_counts.pending ?? status_counts.submitted ?? 0 };
}

/** An older active snapshot must not undo a terminal event already observed. */
export function snapshotParentState(existing: TaskSummary, incoming: TaskSummary): Pick<TaskSummary, "status" | "updated_at"> {
  const terminal = (status: string) => ["completed", "failed", "cancelled"].includes(status);
  if ((existing.status === "cancelled" && incoming.status === "failed")
    || (terminal(existing.status) && !terminal(incoming.status))
    || (Date.parse(existing.updated_at) > Date.parse(incoming.updated_at)
      && (terminal(existing.status) || !terminal(incoming.status)))) {
    return { status: existing.status, updated_at: existing.updated_at };
  }
  return { status: incoming.status, updated_at: incoming.updated_at };
}
