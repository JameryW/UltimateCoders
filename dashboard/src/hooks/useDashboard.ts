import { useState, useCallback } from "react";
import type {
  DashboardSnapshot,
  TaskEvent,
  HealthData,
  WorkersData,
  TasksData,
  TaskSummary,
  SubtaskSummary,
  SchedulerData,
  CircuitBreakerData,
  DashboardEvent,
} from "@/types/dashboard";
import * as api from "@/api/endpoints";

export function useDashboard() {
  const [health, setHealth] = useState<HealthData>({
    available: false,
    status: "unavailable",
    components: [],
  });
  const [workers, setWorkers] = useState<WorkersData>({
    available: false,
    workers: [],
    total: 0,
    available_count: 0,
  });
  const [tasks, setTasks] = useState<TasksData>({
    available: false,
    tasks: [],
    total: 0,
    status_counts: {},
    pending_task_count: 0,
  });
  const [scheduler, setScheduler] = useState<SchedulerData>({
    available: false,
    is_running: false,
    night_window: null,
    jobs: [],
    execution_history: [],
  });
  const [circuitBreaker, setCircuitBreaker] = useState<CircuitBreakerData>({
    available: false,
    circuit_breaker: {
      available: false,
      state: "Unknown",
      failure_count: 0,
      total_calls: 0,
      total_rejected: 0,
    },
    rate_limiter: {
      available: false,
      rpm_available: 0,
      tpm_available: 0,
      active_count: 0,
      total_requests: 0,
    },
    engine_circuit_breaker: {},
    engine_rate_limiter: {},
  });
  const [eventLog, setEventLog] = useState<DashboardEvent[]>([]);
  const [interactionLog, setInteractionLog] = useState<
    Record<string, TaskEvent[]>
  >({});
  const [connected, setConnected] = useState(false);
  const [needsSync, setNeedsSync] = useState(false);

  // Handle SSE full snapshot — merge with existing state to avoid overwriting
  // fresher gRPC-Web incremental updates with stale SSE snapshot data.
  const handleSnapshot = useCallback((data: DashboardSnapshot) => {
    // ponytail: only replace state when SSE source actually has data;
    // in no-NATS mode SSE sends available:false which would wipe gRPC-populated data
    if (data.health?.available) setHealth(data.health);
    if (data.workers?.available) setWorkers(data.workers);
    const snapshotTasks = data.tasks;
    if (snapshotTasks?.available) {
      setTasks((prev) => {
        // SSE available but empty → don't wipe gRPC-populated tasks
        if (snapshotTasks.tasks.length === 0 && prev.available && prev.tasks.length > 0) return prev;
        // Field-level merge: for each task in snapshot, keep the version whose
        // updated_at is more recent (gRPC-Web event updates set updated_at in real-time).
        const merged = snapshotTasks.tasks.map((st) => {
          const existing = prev.tasks.find((t) => t.id === st.id);
          if (!existing) return st;
          // If our existing version is newer (from gRPC-Web event), keep it
          const existingTime = new Date(existing.updated_at).getTime();
          const snapshotTime = new Date(st.updated_at).getTime();
          if (!isNaN(existingTime) && !isNaN(snapshotTime) && existingTime > snapshotTime) {
            return existing;
          }
          // Snapshot is newer or equal → merge subtask-level data
          if (existing.subtasks && st.subtasks) {
            const mergedSubtasks = st.subtasks.map((ss) => {
              const es = existing.subtasks!.find((s) => s.id === ss.id);
              // Keep existing subtask if it has a more advanced status
              if (es) return subtaskStatusRank(es.status) >= subtaskStatusRank(ss.status) ? es : ss;
              return ss;
            });
            return { ...st, subtasks: mergedSubtasks };
          }
          return st;
        });
        // Add tasks from prev that aren't in snapshot (recently submitted via gRPC)
        for (const t of prev.tasks) {
          if (!merged.some((m) => m.id === t.id)) merged.push(t);
        }
        return { ...prev, tasks: merged, total: merged.length };
      });
    }
    if (data.scheduler?.available) setScheduler(data.scheduler);
    if (data.circuit_breaker?.available) setCircuitBreaker(data.circuit_breaker);
    if (data.events && data.events.length > 0) setEventLog(data.events);
  }, []);

  // Handle SSE/gRPC-Web real-time task event
  const handleTaskEvent = useCallback((ev: TaskEvent) => {
    const tid = ev.task_id;
    setInteractionLog((prev) => ({
      ...prev,
      [tid]: [...(prev[tid] ?? []), ev],
    }));
    // Also prepend to event log
    setEventLog((prev) => [
      {
        timestamp: ev.timestamp,
        type: ev.type,
        details: { task_id: ev.task_id, ...ev.data },
      },
      ...prev.slice(0, 199), // keep max 200
    ]);

    switch (ev.type) {
      // -- Sync recovery --
      case "sync_required": {
        // Broadcast lag detected — caller should do a full listTasks re-sync.
        // We don't have listTasks here, so set a flag for App.tsx to act on.
        setNeedsSync(true);
        break;
      }

      // -- Task lifecycle --
      case "task_submitted": {
        setTasks((prev) => {
          if (prev.tasks.some((t) => t.id === tid)) return prev;
          const rawSubtasks = ev.data.subtasks as Array<Record<string, unknown>> | undefined;
          const subtasks: SubtaskSummary[] = rawSubtasks?.map((s) => ({
            id: String(s.id ?? ""),
            description: String(s.description ?? ""),
            status: String(s.status ?? "pending"),
            depends_on: (s.depends_on as string[]) ?? [],
          })) ?? [];
          const tasks = [
            {
              id: tid,
              description: String(ev.data.description ?? ""),
              status: "in_progress",
              project_id: String(ev.data.project_id ?? ""),
              subtask_count: subtasks.length || Number(ev.data.subtask_count ?? 0),
              subtasks: subtasks.length > 0 ? subtasks : undefined,
              created_at: ev.timestamp,
              updated_at: ev.timestamp,
            },
            ...prev.tasks,
          ];
          return { ...prev, tasks, total: tasks.length, status_counts: recountStatus(tasks) };
        });
        break;
      }
      case "task_completed": {
        setTasks((prev) => {
          const tasks = prev.tasks.map((t) =>
            t.id === tid ? { ...t, status: "completed" as const, updated_at: ev.timestamp } : t
          );
          return { ...prev, tasks, status_counts: recountStatus(tasks) };
        });
        break;
      }
      case "task_failed": {
        setTasks((prev) => {
          const tasks = prev.tasks.map((t) =>
            t.id === tid ? { ...t, status: "failed" as const, updated_at: ev.timestamp } : t
          );
          return { ...prev, tasks, status_counts: recountStatus(tasks) };
        });
        break;
      }
      case "task_paused": {
        setTasks((prev) => {
          const tasks = prev.tasks.map((t) =>
            t.id === tid ? { ...t, status: "paused" as const, updated_at: ev.timestamp } : t
          );
          return { ...prev, tasks, status_counts: recountStatus(tasks) };
        });
        break;
      }
      case "task_resumed": {
        setTasks((prev) => {
          const tasks = prev.tasks.map((t) =>
            t.id === tid ? { ...t, status: "in_progress" as const, updated_at: ev.timestamp } : t
          );
          return { ...prev, tasks, status_counts: recountStatus(tasks) };
        });
        break;
      }

      // -- Subtask lifecycle --
      case "subtask_assigned":
      case "subtask_started": {
        setTasks((prev) => ({
          ...prev,
          tasks: prev.tasks.map((t) => {
            if (t.id !== tid) return t;
            const subtasks = mergeSubtaskEvent(t.subtasks ?? [], ev);
            return { ...t, status: "in_progress" as const, subtasks, subtask_count: subtasks.length, updated_at: ev.timestamp };
          }),
        }));
        break;
      }
      case "subtask_completed": {
        setTasks((prev) => ({
          ...prev,
          tasks: prev.tasks.map((t) => {
            if (t.id !== tid) return t;
            const subtasks = mergeSubtaskEvent(t.subtasks ?? [], ev);
            return { ...t, subtasks, subtask_count: subtasks.length, updated_at: ev.timestamp };
          }),
        }));
        break;
      }
      case "subtask_failed": {
        setTasks((prev) => ({
          ...prev,
          tasks: prev.tasks.map((t) => {
            if (t.id !== tid) return t;
            const subtasks = mergeSubtaskEvent(t.subtasks ?? [], ev);
            // If unrecoverable, mark parent task failed too
            const taskFailed = ev.data.recoverable === "false" || ev.data.recoverable === false;
            return { ...t, status: taskFailed ? "failed" as const : t.status, subtasks, subtask_count: subtasks.length, updated_at: ev.timestamp };
          }),
        }));
        break;
      }
    }
  }, []);

  // Fetch initial data — always fetch everything; gRPC-Web merges separately
  const fetchInitial = useCallback(async () => {
    try {
      const h = await api.getHealth();
      setHealth(h);
    } catch { /* ignore */ }
    try {
      const w = await api.getWorkers();
      setWorkers(w);
    } catch { /* ignore */ }
    try {
      const t = await api.getTasks();
      setTasks(t);
    } catch { /* ignore */ }
    try {
      const s = await api.getScheduler();
      setScheduler(s);
    } catch { /* ignore */ }
    try {
      const c = await api.getCircuitBreaker();
      setCircuitBreaker(c);
    } catch { /* ignore */ }
    try {
      const e = await api.getEvents();
      setEventLog(e.events);
    } catch { /* ignore */ }
  }, []);

  /** Merge task list from gRPC-Web into state — field-level merge preserving incremental subtask updates. */
  const mergeGrpcTasks = useCallback((data: TasksData) => {
    setTasks((prev) => {
      if (!data.available) return prev;
      const merged = [...prev.tasks];
      for (const t of data.tasks) {
        const idx = merged.findIndex((m) => m.id === t.id);
        if (idx >= 0) {
          const existing = merged[idx];
          // ponytail: don't spread subtasks — keep the version with more entries
          // (gRPC listTasks snapshot may be stale vs real-time subtask events)
          const subtasks = (existing.subtasks?.length ?? 0) >= (t.subtasks?.length ?? 0)
            ? existing.subtasks : t.subtasks;
          merged[idx] = { ...existing, ...t, subtasks };
        } else {
          merged.push(t);
        }
      }
      return { ...prev, tasks: merged, total: merged.length, status_counts: recountStatus(merged) };
    });
  }, []);

  return {
    health,
    workers,
    tasks,
    scheduler,
    circuitBreaker,
    eventLog,
    interactionLog,
    connected,
    setConnected,
    needsSync,
    setNeedsSync,
    handleSnapshot,
    handleTaskEvent,
    mergeGrpcTasks,
    fetchInitial,
  };
}

/** Rank subtask status for comparison — higher = more advanced. */
function subtaskStatusRank(status: string): number {
  switch (status) {
    case "completed": return 4;
    case "failed": return 4;
    case "in_progress": return 3;
    case "assigned": return 2;
    case "pending": return 1;
    default: return 0;
  }
}

/** Recompute status_counts from a task list. */
function recountStatus(tasks: TaskSummary[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const t of tasks) counts[t.status] = (counts[t.status] || 0) + 1;
  return counts;
}

/** Merge a subtask event into existing subtask list, upserting by subtask_id. */
function mergeSubtaskEvent(subtasks: SubtaskSummary[], ev: TaskEvent): SubtaskSummary[] {
  const sid = ev.subtask_id;
  if (!sid) return subtasks;

  const statusMap: Record<string, string> = {
    subtask_assigned: "assigned",
    subtask_started: "in_progress",
    subtask_completed: "completed",
    subtask_failed: "failed",
  };
  const newStatus = statusMap[ev.type];
  if (!newStatus) return subtasks;

  const existing = subtasks.find((s) => s.id === sid);
  if (existing) {
    return subtasks.map((s) =>
      s.id === sid ? { ...s, status: newStatus } : s,
    );
  }
  return [
    ...subtasks,
    {
      id: sid,
      description: String(ev.data.description ?? ""),
      status: newStatus,
      depends_on: (ev.data.depends_on as string[]) ?? [],
    },
  ];
}
