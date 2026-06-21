import { useState, useCallback } from "react";
import type {
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

/** #12: Maximum number of task entries to keep in interactionLog.
 *  Oldest entries are evicted when this limit is exceeded. */
const INTERACTION_LOG_MAX_ENTRIES = 50;

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
  /** #6: Track errors from fetchInitial so callers can surface them in the UI. */
  const [fetchErrors, setFetchErrors] = useState<Record<string, string>>({});

  // Handle WatchDashboard snapshot -- merge with existing state to avoid overwriting
  // fresher gRPC-Web incremental updates with stale snapshot data.
  const handleSnapshot = useCallback((data: {
    health?: HealthData;
    workers?: WorkersData;
    scheduler?: SchedulerData;
    circuitBreaker?: CircuitBreakerData;
    events?: DashboardEvent[];
  }) => {
    if (data.health?.available) setHealth(data.health);
    if (data.workers?.available) setWorkers(data.workers);
    if (data.scheduler?.available) setScheduler(data.scheduler);
    if (data.circuit_breaker?.available) setCircuitBreaker(data.circuit_breaker);
    if (data.events && data.events.length > 0) setEventLog(data.events);
  }, []);

  // Handle SSE/gRPC-Web real-time task event
  const handleTaskEvent = useCallback((ev: TaskEvent) => {
    const tid = ev.task_id;
    setInteractionLog((prev) => {
      const updated = {
        ...prev,
        [tid]: [...(prev[tid] ?? []), ev],
      };
      // #12: LRU eviction -- keep max INTERACTION_LOG_MAX_ENTRIES task entries.
      // Evict oldest (by first event timestamp) when exceeded.
      const keys = Object.keys(updated);
      if (keys.length > INTERACTION_LOG_MAX_ENTRIES) {
        // Sort by the timestamp of the first event in each entry
        const sorted = keys.sort((a, b) => {
          const ta = updated[a]?.[0]?.timestamp ?? "";
          const tb = updated[b]?.[0]?.timestamp ?? "";
          return ta.localeCompare(tb);
        });
        const toEvict = sorted.slice(0, keys.length - INTERACTION_LOG_MAX_ENTRIES);
        for (const k of toEvict) delete updated[k];
      }
      return updated;
    });
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
        // Broadcast lag detected -- caller should do a full listTasks re-sync.
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
              // #6: Use ev.data.status first, fallback to "submitted" (not "in_progress")
              status: String(ev.data.status ?? "submitted"),
              project_id: String(ev.data.project_id ?? ""),
              subtask_count: subtasks.length || Number(ev.data.subtask_count ?? 0),
              subtasks: subtasks.length > 0 ? subtasks : undefined,
              created_at: ev.timestamp,
              updated_at: ev.timestamp,
            },
            ...prev.tasks,
          ];
          const status_counts = recountStatus(tasks);
          return { ...prev, tasks, total: totalFromStatusCounts(status_counts), status_counts };
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

  // Fetch initial data via gRPC-Web; optionally skip tasks (WatchTask stream will provide them)
  // #6: Track errors instead of silently swallowing them; expose via fetchErrors state.
  // All requests run in parallel for faster initial load.
  const fetchInitial = useCallback(async (opts?: {
    skipTasks?: boolean;
    fetchWorkers?: () => Promise<WorkersData>;
    fetchScheduler?: () => Promise<SchedulerData>;
    fetchCircuitBreaker?: () => Promise<CircuitBreakerData>;
    fetchEvents?: (taskId?: string, limit?: number) => Promise<{ available: boolean; events: DashboardEvent[]; total: number }>;
    fetchTasks?: () => Promise<TasksData>;
  }): Promise<Record<string, string>> => {
    const errors: Record<string, string> = {};
    const results = await Promise.allSettled([
      opts?.fetchWorkers?.().then((w) => { setWorkers(w); }).catch((e) => { errors["workers"] = String(e); }) ?? Promise.resolve(),
      opts?.skipTasks
        ? Promise.resolve()
        : opts?.fetchTasks?.().then((t) => { setTasks(t); }).catch((e) => { errors["tasks"] = String(e); }) ?? Promise.resolve(),
      opts?.fetchScheduler?.().then((s) => { setScheduler(s); }).catch((e) => { errors["scheduler"] = String(e); }) ?? Promise.resolve(),
      opts?.fetchCircuitBreaker?.().then((c) => { setCircuitBreaker(c); }).catch((e) => { errors["circuit_breaker"] = String(e); }) ?? Promise.resolve(),
      opts?.fetchEvents?.().then((e) => { setEventLog(e.events); }).catch((e) => { errors["events"] = String(e); }) ?? Promise.resolve(),
    ]);
    // results are handled via .then/.catch above; Promise.allSettled just waits for all
    void results;
    setFetchErrors(errors);
    return errors;
  }, []);

  /** Merge task list from gRPC-Web into state -- field-level merge preserving incremental subtask updates. */
  const mergeGrpcTasks = useCallback((data: TasksData) => {
    setTasks((prev) => {
      if (!data.available) return prev;
      const merged = [...prev.tasks];
      for (const t of data.tasks) {
        const idx = merged.findIndex((m) => m.id === t.id);
        if (idx >= 0) {
          const existing = merged[idx]!;
          // Deep-merge subtasks: for each subtask, keep the version with more advanced status
          let subtasks = t.subtasks;
          if (existing.subtasks && t.subtasks) {
            subtasks = t.subtasks.map((gs) => {
              const es = existing.subtasks!.find((s) => s.id === gs.id);
              if (es) return subtaskStatusRank(es.status) >= subtaskStatusRank(gs.status) ? es : gs;
              return gs;
            });
            // Keep subtasks from existing that aren't in gRPC response
            for (const es of existing.subtasks) {
              if (!subtasks.some((s) => s.id === es.id)) subtasks.push(es);
            }
          } else if (existing.subtasks && !t.subtasks) {
            // gRPC response has no subtasks -- keep existing
            subtasks = existing.subtasks;
          }
          merged[idx] = { ...existing, ...t, subtasks };
        } else {
          merged.push(t);
        }
      }
      // #15: Derive total from status_counts for consistency
      const status_counts = recountStatus(merged);
      return { ...prev, tasks: merged, total: totalFromStatusCounts(status_counts), status_counts };
    });
  }, []);

  /** Optimistic insert: add a task before SSE/gRPC event arrives. */
  const optimisticAddTask = useCallback((taskId: string, description: string, projectId: string, subtaskCount: number, subtasks?: SubtaskSummary[]) => {
    setTasks((prev) => {
      if (prev.tasks.some((t) => t.id === taskId)) return prev;
      const newTask: TaskSummary = {
        id: taskId,
        description,
        status: "in_progress",
        project_id: projectId,
        subtask_count: subtaskCount,
        subtasks,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      const tasks = [newTask, ...prev.tasks];
      const status_counts = recountStatus(tasks);
      return { ...prev, tasks, total: totalFromStatusCounts(status_counts), status_counts };
    });
  }, []);

  /** #13: Optimistic status update -- immediately update task status before
   *  SSE/gRPC event confirms it. The real event will correct it if wrong. */
  const optimisticStatusUpdate = useCallback((taskId: string, newStatus: string) => {
    setTasks((prev) => {
      const tasks = prev.tasks.map((t) =>
        t.id === taskId ? { ...t, status: newStatus, updated_at: new Date().toISOString() } : t
      );
      return { ...prev, tasks, status_counts: recountStatus(tasks) };
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
    fetchErrors,
    handleSnapshot,
    handleTaskEvent,
    mergeGrpcTasks,
    fetchInitial,
    optimisticAddTask,
    optimisticStatusUpdate,
  };
}

/** Rank subtask status for comparison -- higher = more advanced. */
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

/** Recompute status_counts from a task list.
 *  #15: Also returns total as the sum of status_counts for consistency. */
function recountStatus(tasks: TaskSummary[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const t of tasks) counts[t.status] = (counts[t.status] || 0) + 1;
  return counts;
}

/** #15: Derive total from status_counts to avoid inconsistency
 *  between merged.length total and status_counts total. */
function totalFromStatusCounts(counts: Record<string, number>): number {
  let total = 0;
  for (const v of Object.values(counts)) total += v;
  return total;
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

