import { useState, useCallback } from "react";
import type {
  DashboardSnapshot,
  TaskEvent,
  HealthData,
  WorkersData,
  TasksData,
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

  // Handle SSE full snapshot
  const handleSnapshot = useCallback((data: DashboardSnapshot) => {
    if (data.health) setHealth(data.health);
    if (data.workers) setWorkers(data.workers);
    if (data.tasks) setTasks(data.tasks);
    if (data.scheduler) setScheduler(data.scheduler);
    if (data.circuit_breaker) setCircuitBreaker(data.circuit_breaker);
    if (data.events) setEventLog(data.events);
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
    // When a new task appears via gRPC-Web that's not in our task list, add a placeholder
    if (ev.type === "task_submitted") {
      setTasks((prev) => {
        if (prev.tasks.some((t) => t.id === tid)) return prev;
        return {
          ...prev,
          tasks: [
            {
              id: tid,
              description: String(ev.data.description ?? ""),
              status: "in_progress",
              project_id: String(ev.data.project_id ?? ""),
              subtask_count: 0,
              created_at: ev.timestamp,
              updated_at: ev.timestamp,
            },
            ...prev.tasks,
          ],
          total: prev.total + 1,
        };
      });
    }
    // Update subtask counts from events
    if (ev.type === "subtask_assigned" || ev.type === "subtask_started") {
      setTasks((prev) => ({
        ...prev,
        tasks: prev.tasks.map((t) =>
          t.id === tid ? { ...t, status: "in_progress" as const, updated_at: ev.timestamp } : t,
        ),
      }));
    }
    if (ev.type === "task_completed") {
      setTasks((prev) => ({
        ...prev,
        tasks: prev.tasks.map((t) =>
          t.id === tid ? { ...t, status: "completed" as const, updated_at: ev.timestamp } : t,
        ),
      }));
    }
    if (ev.type === "subtask_failed" && ev.data.recoverable === "false") {
      setTasks((prev) => ({
        ...prev,
        tasks: prev.tasks.map((t) =>
          t.id === tid ? { ...t, status: "failed" as const, updated_at: ev.timestamp } : t,
        ),
      }));
    }
  }, []);

  // Fetch initial data
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

  // Merge gRPC Health components (local_worker, engine, etc.) into existing health state
  const mergeGrpcComponents = useCallback(
    (components: { name: string; status: string; details?: string }[]) => {
      setHealth((prev) => {
        // Merge: keep existing Python components, add/update gRPC ones (prefixed)
        const existingNames = new Set(prev.components.map((c) => c.name));
        const grpcComps = components.map((c) => ({
          name: existingNames.has(c.name) ? `gRPC: ${c.name}` : c.name,
          status: c.status,
          details: c.details,
        }));
        return {
          ...prev,
          available: true,
          components: [...prev.components.filter((c) => !c.name.startsWith("gRPC:")), ...grpcComps],
        };
      });
    },
    [],
  );

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
    handleSnapshot,
    handleTaskEvent,
    fetchInitial,
    mergeGrpcComponents,
  };
}
