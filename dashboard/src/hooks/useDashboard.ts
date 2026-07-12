import { useState, useCallback, useRef } from "react";
import type {
  TaskEvent,
  HealthData,
  WorkersData,
  TasksData,
  TaskSummary,
  SubtaskSummary,
  SchedulerData,
  DashboardEvent,
  MetricsSnapshot,
  AlertEvent,
} from "@/types/dashboard";

/** #12: Maximum number of task entries to keep in interactionLog.
 *  Oldest entries are evicted when this limit is exceeded. */
const INTERACTION_LOG_MAX_ENTRIES = 50;

/** Dedup window in ms — events with the same key within this window are dropped. */
const DEDUP_WINDOW_MS = 5000;

/** Build a dedup key for a task event. */
function dedupEventKey(ev: TaskEvent): string {
  return `${ev.type}:${ev.task_id}:${ev.subtask_id ?? ""}:${ev.timestamp}`;
}

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
  const [eventLog, setEventLog] = useState<DashboardEvent[]>([]);
  const [interactionLog, setInteractionLog] = useState<
    Record<string, TaskEvent[]>
  >({});
  const [needsSync, setNeedsSync] = useState(false);
  /** #6: Track errors from fetchInitial so callers can surface them in the UI. */
  const [fetchErrors, setFetchErrors] = useState<Record<string, string>>({});
  const [metrics, setMetrics] = useState<MetricsSnapshot | null>(null);
  const [activeAlerts, setActiveAlerts] = useState<AlertEvent[]>([]);
  const [resolvedAlertTypes, setResolvedAlertTypes] = useState<string[]>([]);
  /** Dedup: Map<dedupKey, timestamp> — events within DEDUP_WINDOW_MS are dropped. */
  const dedupRef = useRef<Map<string, number>>(new Map());

  // Handle WatchDashboard snapshot -- merge with existing state to avoid overwriting
  // fresher gRPC-Web incremental updates with stale snapshot data.
  const handleSnapshot = useCallback((data: {
    health?: HealthData;
    workers?: WorkersData;
    scheduler?: SchedulerData;
    events?: DashboardEvent[];
    metrics?: MetricsSnapshot;
    alert_events?: AlertEvent[];
    alert_resolved?: string[];
  }) => {
    if (data.health?.available) setHealth(data.health);
    if (data.workers?.available) setWorkers(data.workers);
    if (data.scheduler?.available) setScheduler(data.scheduler);
    if (data.events && data.events.length > 0) setEventLog(data.events);
    if (data.metrics) setMetrics(data.metrics);
    if (data.alert_events && data.alert_events.length > 0) {
      setActiveAlerts(prev => [...prev, ...data.alert_events!].slice(-50));
    }
    if (data.alert_resolved && data.alert_resolved.length > 0) {
      setResolvedAlertTypes(data.alert_resolved);
      setActiveAlerts(prev => prev.filter(a => !data.alert_resolved!.includes(a.alert_type)));
    }
  }, []);

  // Handle SSE/gRPC-Web real-time task event (with dedup)
  const handleTaskEvent = useCallback((ev: TaskEvent) => {
    // ── Dedup: drop events we've already processed within the window ──
    const key = dedupEventKey(ev);
    const now = Date.now();
    const seen = dedupRef.current.get(key);
    if (seen !== undefined && now - seen < DEDUP_WINDOW_MS) {
      return; // duplicate — skip
    }
    dedupRef.current.set(key, now);
    // Prune old entries periodically (every 100 events)
    if (dedupRef.current.size > 200) {
      for (const [k, t] of dedupRef.current) {
        if (now - t > DEDUP_WINDOW_MS) dedupRef.current.delete(k);
      }
    }

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
            assigned_worker: s.assigned_worker ? String(s.assigned_worker) : undefined,
            result: s.result ? String(s.result) : undefined,
            modified_files: Array.isArray(s.modified_files)
              ? (s.modified_files as Array<Record<string, unknown>>).map((f) => ({
                  path: String(f.path ?? f.file_path ?? ""),
                  type: String(f.type ?? f.change_type ?? "modify"),
                }))
              : undefined,
            retry_count: s.retry_count != null ? Number(s.retry_count) : undefined,
            error: s.error ? String(s.error) : undefined,
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
      case "subtask_retrying": {
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
      case "subtask_progress": {
        // Progress events must NOT change subtask status — only refresh
        // phase/percent/step_* fields on the existing (in_progress) subtask.
        setTasks((prev) => ({
          ...prev,
          tasks: prev.tasks.map((t) => {
            if (t.id !== tid) return t;
            const subtasks = mergeProgressEvent(t.subtasks ?? [], ev);
            return subtasks === t.subtasks ? t : { ...t, subtasks };
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
    eventLog,
    interactionLog,
    metrics,
    activeAlerts,
    resolvedAlertTypes,
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
    case "completed": return 5;
    case "failed": return 5;
    case "in_progress": return 4;
    case "retrying": return 3;
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
    subtask_retrying: "retrying",
  };
  const newStatus = statusMap[ev.type];
  if (!newStatus) return subtasks;

  const existing = subtasks.find((s) => s.id === sid);
  // ponytail: carry assigned_worker from event data (set by subtask_assigned/subtask_started)
  const evWorker = ev.data.assigned_worker ? String(ev.data.assigned_worker) : undefined;
  // Carry result from completed events: prefer full result text, fallback to summary
  const evResult = ev.data.result ? String(ev.data.result) : ev.data.summary ? String(ev.data.summary) : undefined;
  // Carry modified_files from completed events
  const evModifiedFiles = Array.isArray(ev.data.modified_files)
    ? (ev.data.modified_files as Array<Record<string, unknown>>).map((f) => ({
        path: String(f.path ?? f.file_path ?? ""),
        type: String(f.type ?? f.change_type ?? "modify"),
      }))
    : undefined;
  // Carry retry_count from retrying events
  const evRetryCount = ev.data.retry_count != null ? Number(ev.data.retry_count) : undefined;
  // Carry error from failed events
  const evError = ev.data.error ? String(ev.data.error) : undefined;

  if (existing) {
    return subtasks.map((s) =>
      s.id === sid ? {
        ...s,
        status: newStatus,
        assigned_worker: evWorker ?? s.assigned_worker,
        result: evResult ?? s.result,
        modified_files: evModifiedFiles ?? s.modified_files,
        retry_count: evRetryCount ?? s.retry_count,
        error: evError ?? s.error,
      } : s,
    );
  }
  return [
    ...subtasks,
    {
      id: sid,
      description: String(ev.data.description ?? ""),
      status: newStatus,
      depends_on: (ev.data.depends_on as string[]) ?? [],
      assigned_worker: evWorker,
      result: evResult,
      modified_files: evModifiedFiles,
      retry_count: evRetryCount,
      error: evError,
    },
  ];
}

/** Merge a subtask_progress event into existing subtask list, upserting by subtask_id.
 *  Unlike mergeSubtaskEvent, this does NOT change subtask status — it only
 *  refreshes the real-time phase/percent/step_* fields. If the subtask is
 *  unknown, it is ignored (progress only makes sense for an existing subtask). */
function mergeProgressEvent(subtasks: SubtaskSummary[], ev: TaskEvent): SubtaskSummary[] {
  const sid = ev.subtask_id;
  if (!sid) return subtasks;

  const phase = ev.data.phase != null ? String(ev.data.phase) : undefined;
  const percent = ev.data.percent != null ? Number(ev.data.percent) : undefined;
  const step_agent = ev.data.step_agent != null ? String(ev.data.step_agent) : undefined;
  const step_status = ev.data.step_status != null ? String(ev.data.step_status) : undefined;
  const step_index = ev.data.step_index != null ? Number(ev.data.step_index) : undefined;
  const step_total = ev.data.step_total != null ? Number(ev.data.step_total) : undefined;
  const step_summary = ev.data.step_summary != null ? String(ev.data.step_summary) : undefined;

  const existing = subtasks.find((s) => s.id === sid);
  if (!existing) return subtasks; // progress for an unknown subtask — ignore

  return subtasks.map((s) =>
    s.id === sid ? {
      ...s,
      phase: phase ?? s.phase,
      percent: percent ?? s.percent,
      step_agent: step_agent ?? s.step_agent,
      step_status: step_status ?? s.step_status,
      step_index: step_index ?? s.step_index,
      step_total: step_total ?? s.step_total,
      step_summary: step_summary ?? s.step_summary,
    } : s,
  );
}

