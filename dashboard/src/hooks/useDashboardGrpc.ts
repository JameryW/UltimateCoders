import { useEffect, useRef, useCallback, useState } from "react";
import { createClient } from "@connectrpc/connect";
import { create } from "@bufbuild/protobuf";
import { DashboardService } from "@/grpc/engine_pb";
import {
  ListWorkersRequestSchema,
  GetSchedulerStatusRequestSchema,
  GetCircuitBreakerStatusRequestSchema,
  ResetCircuitBreakerRequestSchema,
  TriggerSchedulerJobRequestSchema,
  FlushPendingTasksRequestSchema,
  ListEventsRequestSchema,
  WatchDashboardRequestSchema,
} from "@/grpc/engine_pb";
import type {
  DashboardEventProto,
  TaskEvent as GrpcTaskEvent,
  ListWorkersResponse,
  GetSchedulerStatusResponse,
  CircuitBreakerStatusResponse,
  WorkerProto,
  ScheduledJobProto,
  NightWindowProto,
  ExecutionHistoryProto,
  CircuitBreakerProto,
  RateLimiterProto,
  HealthSnapshot,
} from "@/grpc/engine_pb";
import type {
  HealthData,
  WorkersData,
  WorkerInfo,
  SchedulerData,
  NightWindow,
  ScheduledJob,
  ExecutionHistory,
  CircuitBreakerData,
  CircuitBreakerInfo,
  RateLimiterInfo,
  DashboardEvent,
  TaskEvent,
  TasksData,
} from "@/types/dashboard";
import { getSharedTransport } from "@/hooks/useGrpcWeb";

// ── gRPC-Web -> Dashboard type converters ────────────────────

function grpcHealthToDashboard(hs: HealthSnapshot): HealthData {
  return {
    available: hs.available,
    status: hs.status,
    version: hs.version ?? undefined,
    uptime_seconds: hs.uptimeSeconds != null ? Number(hs.uptimeSeconds) : undefined,
    components: [],
  };
}

function grpcWorkerToDashboard(w: WorkerProto): WorkerInfo {
  return {
    id: w.id,
    capabilities: [...w.capabilities],
    current_load: w.currentLoad,
    max_capacity: w.maxCapacity,
    load_percent: w.loadPercent,
    last_heartbeat: w.lastHeartbeat,
    heartbeat_age_seconds: w.heartbeatAgeSeconds,
    heartbeat_stale: w.heartbeatStale,
    is_available: w.isAvailable,
  };
}

function grpcWorkersToDashboard(resp: ListWorkersResponse): WorkersData {
  return {
    available: resp.available,
    workers: resp.workers.map(grpcWorkerToDashboard),
    total: resp.total,
    available_count: resp.availableCount,
  };
}

function grpcNightWindowToDashboard(nw: NightWindowProto): NightWindow {
  return {
    start: nw.start,
    end: nw.end,
    timezone: "",  // not in proto
    is_active: nw.enabled,
  };
}

function grpcScheduledJobToDashboard(j: ScheduledJobProto): ScheduledJob {
  return {
    id: j.id,
    description: j.name,
    enabled: j.enabled,
    cron_expression: j.cron || undefined,
    execute_after: j.nextRun ?? undefined,
  };
}

function grpcExecutionHistoryToDashboard(e: ExecutionHistoryProto): ExecutionHistory {
  return {
    task_id: e.jobId,
    status: e.success ? "success" : "failed",
    started_at: e.startedAt ?? undefined,
    completed_at: e.completedAt ?? undefined,
    result_summary: e.resultSummary ?? undefined,
  };
}

function grpcSchedulerToDashboard(resp: GetSchedulerStatusResponse): SchedulerData {
  return {
    available: resp.available,
    is_running: resp.isRunning,
    night_window: resp.nightWindow ? grpcNightWindowToDashboard(resp.nightWindow) : null,
    jobs: resp.jobs.map(grpcScheduledJobToDashboard),
    execution_history: resp.executionHistory.map(grpcExecutionHistoryToDashboard),
  };
}

function grpcCircuitBreakerToDashboard(cb: CircuitBreakerProto): CircuitBreakerInfo {
  return {
    available: true,
    state: cb.state,
    failure_count: cb.failureCount,
    total_calls: cb.failureThreshold,
    total_rejected: 0,
  };
}

function grpcRateLimiterToDashboard(rl: RateLimiterProto): RateLimiterInfo {
  return {
    available: true,
    rpm_available: rl.maxRequests,
    tpm_available: 0,
    active_count: rl.currentRequests,
    total_requests: rl.maxRequests,
  };
}

function grpcCircuitBreakerStatusToDashboard(resp: CircuitBreakerStatusResponse): CircuitBreakerData {
  return {
    available: resp.available,
    circuit_breaker: resp.circuitBreaker
      ? grpcCircuitBreakerToDashboard(resp.circuitBreaker)
      : { available: false, state: "Unknown", failure_count: 0, total_calls: 0, total_rejected: 0 },
    rate_limiter: resp.rateLimiter
      ? grpcRateLimiterToDashboard(resp.rateLimiter)
      : { available: false, rpm_available: 0, tpm_available: 0, active_count: 0, total_requests: 0 },
    engine_circuit_breaker: resp.circuitBreaker ?? {},
    engine_rate_limiter: resp.rateLimiter ?? {},
  };
}

function grpcEventProtoToDashboardEvent(ev: DashboardEventProto): DashboardEvent {
  const details: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(ev.data)) {
    if (value.startsWith("[") || value.startsWith("{")) {
      try { details[key] = JSON.parse(value); } catch { details[key] = value; }
    } else if (/^-?\d+(\.\d+)?$/.test(value)) {
      details[key] = Number(value);
    } else if (value === "true" || value === "false") {
      details[key] = value === "true";
    } else {
      details[key] = value;
    }
  }
  return {
    timestamp: ev.timestamp,
    type: ev.type,
    details,
  };
}

function grpcEventProtoToTaskEvent(ev: DashboardEventProto): TaskEvent {
  const data: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(ev.data)) {
    if (value.startsWith("[") || value.startsWith("{")) {
      try { data[key] = JSON.parse(value); } catch { data[key] = value; }
    } else if (/^-?\d+(\.\d+)?$/.test(value)) {
      data[key] = Number(value);
    } else if (value === "true" || value === "false") {
      data[key] = value === "true";
    } else {
      data[key] = value;
    }
  }
  return {
    timestamp: ev.timestamp,
    type: ev.type,
    task_id: ev.taskId,
    data,
  };
}

/** Convert a gRPC TaskEvent proto (from DashboardSnapshot.recent_task_events) to a dashboard TaskEvent. */
function grpcTaskEventToTaskEvent(ev: GrpcTaskEvent): TaskEvent {
  const data: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(ev.data)) {
    if (value.startsWith("[") || value.startsWith("{")) {
      try { data[key] = JSON.parse(value); } catch { data[key] = value; }
    } else if (/^-?\d+(\.\d+)?$/.test(value)) {
      data[key] = Number(value);
    } else if (value === "true" || value === "false") {
      data[key] = value === "true";
    } else {
      data[key] = value;
    }
  }
  return {
    timestamp: ev.timestamp,
    type: ev.type,
    task_id: ev.taskId,
    subtask_id: ev.subtaskId ?? undefined,
    data,
  };
}

// ── Hook interface ────────────────────────────────────────────

export type DashboardConnectionState =
  | "disconnected"
  | "connecting"
  | "connected"
  | "error"
  | "reconnecting";

interface UseDashboardGrpcOptions {
  onSnapshot?: (snapshot: {
    health?: HealthData;
    workers?: WorkersData;
    scheduler?: SchedulerData;
    circuitBreaker?: CircuitBreakerData;
    events?: DashboardEvent[];
  }) => void;
  onTaskEvent?: (event: TaskEvent) => void;
  /** Merge task list from SSE snapshot into dashboard state. */
  mergeGrpcTasks?: (data: TasksData) => void;
  enabled?: boolean;
}

/** Exponential backoff intervals (ms) for reconnection. */
const RETRY_INTERVALS = [1000, 2000, 4000, 8000, 16000, 30000, 60000];
const MAX_RETRY_INTERVAL = 60000;
/** After this many consecutive failures, fall back to SSE. */
const SSE_FALLBACK_THRESHOLD = 5;

export function useDashboardGrpc(opts: UseDashboardGrpcOptions) {
  const [connectionState, setConnectionState] =
    useState<DashboardConnectionState>("disconnected");
  const connectionStateRef = useRef<DashboardConnectionState>(connectionState);
  connectionStateRef.current = connectionState;
  const abortRef = useRef<AbortController | null>(null);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const optsRef = useRef(opts);
  optsRef.current = opts;
  const connectRef = useRef<() => void>(() => {});
  const sseRef = useRef<EventSource | null>(null);
  const usingSseRef = useRef(false);
  const connectSseRef = useRef<() => void>(() => {});

  const getTransport = useCallback(() => getSharedTransport(), []);

  const clearRetryTimer = useCallback(() => {
    if (retryTimerRef.current !== null) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  const scheduleReconnect = useCallback(() => {
    const delay = RETRY_INTERVALS[retryCountRef.current] ?? MAX_RETRY_INTERVAL;
    retryCountRef.current += 1;
    if (import.meta.env.DEV) console.log(`[Dashboard gRPC] Reconnecting in ${delay}ms (attempt ${retryCountRef.current})`);
    setConnectionState("reconnecting");

    // Fallback to SSE after too many gRPC failures
    if (retryCountRef.current >= SSE_FALLBACK_THRESHOLD && !usingSseRef.current) {
      if (import.meta.env.DEV) console.log("[Dashboard] gRPC failed repeatedly, falling back to SSE");
      retryTimerRef.current = setTimeout(() => {
        if (optsRef.current.enabled) {
          connectSseRef.current();
        }
      }, 1000);
      return;
    }

    retryTimerRef.current = setTimeout(() => {
      if (optsRef.current.enabled) {
        connectRef.current();
      }
    }, delay);
  }, []);

  // ── SSE fallback ──────────────────────────────────────────

  const connectSse = useCallback(() => {
    sseRef.current?.close();
    usingSseRef.current = true;

    const sse = new EventSource("/dashboard/api/stream");
    sseRef.current = sse;
    setConnectionState("connected");

    sse.addEventListener("task_event", (e) => {
      try {
        const data = JSON.parse(e.data);
        const taskEvent: TaskEvent = {
          timestamp: data.timestamp ?? new Date().toISOString(),
          type: data.type ?? "",
          task_id: data.task_id ?? "",
          subtask_id: data.subtask_id ?? undefined,
          data: data.data ?? {},
          _sseId: e.lastEventId || undefined,
        };
        optsRef.current.onTaskEvent?.(taskEvent);
      } catch { /* ignore parse errors */ }
    });

    sse.addEventListener("update", (e) => {
      try {
        const snapshot = JSON.parse(e.data);
        const converted: {
          health?: HealthData;
          workers?: WorkersData;
          scheduler?: SchedulerData;
          circuitBreaker?: CircuitBreakerData;
          events?: DashboardEvent[];
        } = {};
        if (snapshot.health?.available) converted.health = snapshot.health;
        if (snapshot.workers?.available) converted.workers = snapshot.workers;
        if (snapshot.scheduler?.available) converted.scheduler = snapshot.scheduler;
        if (snapshot.circuit_breaker?.available) converted.circuitBreaker = snapshot.circuit_breaker;
        if (snapshot.tasks) {
          // Merge task list from SSE snapshot
          if (optsRef.current.mergeGrpcTasks) {
            optsRef.current.mergeGrpcTasks(snapshot.tasks as TasksData);
          }
        }
        optsRef.current.onSnapshot?.(converted);
      } catch { /* ignore */ }
    });

    sse.onerror = () => {
      if (import.meta.env.DEV) console.log("[Dashboard SSE] Connection lost, reconnecting...");
      setConnectionState("error");
      sse.close();
      sseRef.current = null;
      usingSseRef.current = false;
      scheduleReconnect();
    };
  }, [scheduleReconnect]);

  connectSseRef.current = connectSse;

  // ── WatchDashboard stream ─────────────────────────────────

  const connect = useCallback(() => {
    abortRef.current?.abort();
    clearRetryTimer();
    retryCountRef.current = 0;
    sseRef.current?.close();
    sseRef.current = null;
    usingSseRef.current = false;

    const ac = new AbortController();
    abortRef.current = ac;

    if (!optsRef.current.enabled) {
      setConnectionState("disconnected");
      return;
    }

    setConnectionState("connecting");

    const transport = getTransport();
    const client = createClient(DashboardService, transport);
    const req = create(WatchDashboardRequestSchema, {});

    (async () => {
      try {
        const stream = client.watchDashboard(req, { signal: ac.signal });
        setConnectionState("connected");
        retryCountRef.current = 0;

        for await (const snapshot of stream) {
          if (ac.signal.aborted) break;

          const converted: {
            health?: HealthData;
            workers?: WorkersData;
            scheduler?: SchedulerData;
            circuitBreaker?: CircuitBreakerData;
            events?: DashboardEvent[];
          } = {};

          if (snapshot.health) {
            converted.health = grpcHealthToDashboard(snapshot.health);
          }
          if (snapshot.workers) {
            converted.workers = grpcWorkersToDashboard(snapshot.workers);
          }
          if (snapshot.scheduler) {
            converted.scheduler = grpcSchedulerToDashboard(snapshot.scheduler);
          }
          if (snapshot.circuitBreaker) {
            converted.circuitBreaker = grpcCircuitBreakerStatusToDashboard(snapshot.circuitBreaker);
          }
          if (snapshot.recentEvents.length > 0) {
            converted.events = snapshot.recentEvents.map(grpcEventProtoToDashboardEvent);
          }

          optsRef.current.onSnapshot?.(converted);

          // Also emit task events from recent_events for real-time updates
          for (const ev of snapshot.recentEvents) {
            const taskEvent = grpcEventProtoToTaskEvent(ev);
            optsRef.current.onTaskEvent?.(taskEvent);
          }

          // Emit fine-grained task events from recent_task_events (gRPC TaskEvent protos)
          for (const ev of snapshot.recentTaskEvents) {
            const taskEvent = grpcTaskEventToTaskEvent(ev);
            optsRef.current.onTaskEvent?.(taskEvent);
          }
        }

        // Stream ended normally (server closed) -- reconnect
        if (!ac.signal.aborted) {
          setConnectionState("error");
          scheduleReconnect();
        }
      } catch (err: unknown) {
        if (ac.signal.aborted) return;
        console.error("[Dashboard gRPC] WatchDashboard stream error:", err);
        setConnectionState("error");
        scheduleReconnect();
      }
    })();
  }, [clearRetryTimer, scheduleReconnect]);

  connectRef.current = connect;

  const disconnect = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    sseRef.current?.close();
    sseRef.current = null;
    usingSseRef.current = false;
    clearRetryTimer();
    retryCountRef.current = 0;
    setConnectionState("disconnected");
  }, [clearRetryTimer]);

  // ── Unary RPCs ────────────────────────────────────────────

  const listWorkers = useCallback(async (): Promise<WorkersData> => {
    const transport = getTransport();
    const client = createClient(DashboardService, transport);
    const resp = await client.listWorkers(create(ListWorkersRequestSchema, {}));
    return grpcWorkersToDashboard(resp);
  }, []);

  const getSchedulerStatus = useCallback(async (): Promise<SchedulerData> => {
    const transport = getTransport();
    const client = createClient(DashboardService, transport);
    const resp = await client.getSchedulerStatus(create(GetSchedulerStatusRequestSchema, {}));
    return grpcSchedulerToDashboard(resp);
  }, []);

  const getCircuitBreakerStatus = useCallback(async (): Promise<CircuitBreakerData> => {
    const transport = getTransport();
    const client = createClient(DashboardService, transport);
    const resp = await client.getCircuitBreakerStatus(create(GetCircuitBreakerStatusRequestSchema, {}));
    return grpcCircuitBreakerStatusToDashboard(resp);
  }, []);

  const resetCircuitBreaker = useCallback(async (): Promise<{ success: boolean; error?: string }> => {
    const transport = getTransport();
    const client = createClient(DashboardService, transport);
    const resp = await client.resetCircuitBreaker(create(ResetCircuitBreakerRequestSchema, {}));
    return { success: resp.success, error: resp.error ?? undefined };
  }, []);

  const triggerSchedulerJob = useCallback(async (jobId: string): Promise<{ success: boolean; error?: string }> => {
    const transport = getTransport();
    const client = createClient(DashboardService, transport);
    const resp = await client.triggerSchedulerJob(create(TriggerSchedulerJobRequestSchema, { jobId }));
    return { success: resp.success, error: resp.error ?? undefined };
  }, []);

  const flushPendingTasks = useCallback(async (): Promise<{ success: boolean; pendingCount: number; executedCount: number; error?: string }> => {
    const transport = getTransport();
    const client = createClient(DashboardService, transport);
    const resp = await client.flushPendingTasks(create(FlushPendingTasksRequestSchema, {}));
    return { success: resp.success, pendingCount: resp.pendingCount, executedCount: resp.executedCount, error: resp.error ?? undefined };
  }, []);

  const listEvents = useCallback(async (taskId?: string, limit = 100): Promise<{ available: boolean; events: DashboardEvent[]; total: number }> => {
    const transport = getTransport();
    const client = createClient(DashboardService, transport);
    const resp = await client.listEvents(create(ListEventsRequestSchema, {
      taskId: taskId ?? undefined,
      limit,
      offset: 0,
    }));
    return {
      available: resp.available,
      events: resp.events.map(grpcEventProtoToDashboardEvent),
      total: resp.total,
    };
  }, []);

  // ── Lifecycle ─────────────────────────────────────────────

  useEffect(() => {
    connect();
    return disconnect;
  }, [connect, disconnect]);

  return {
    connectionState,
    connect,
    disconnect,
    listWorkers,
    getSchedulerStatus,
    getCircuitBreakerStatus,
    resetCircuitBreaker,
    triggerSchedulerJob,
    flushPendingTasks,
    listEvents,
  };
}
