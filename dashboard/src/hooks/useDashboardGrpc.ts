import { useEffect, useRef, useCallback, useState } from "react";
import { createClient } from "@connectrpc/connect";
import { create } from "@bufbuild/protobuf";
import { DashboardService } from "@/grpc/engine_pb";
import {
  ListWorkersRequestSchema,
  GetSchedulerStatusRequestSchema,
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
  WorkerProto,
  ScheduledJobProto,
  NightWindowProto,
  ExecutionHistoryProto,
  HealthSnapshot,
  MetricsSnapshot as GrpcMetricsSnapshot,
  TaskMetrics as GrpcTaskMetrics,
  WorkerMetrics as GrpcWorkerMetrics,
  EventMetrics as GrpcEventMetrics,
  SystemMetrics as GrpcSystemMetrics,
  MetricsSample as GrpcMetricsSample,
} from "@/grpc/engine_pb";
import type {
  HealthData,
  WorkersData,
  WorkerInfo,
  SchedulerData,
  NightWindow,
  ScheduledJob,
  ExecutionHistory,
  DashboardEvent,
  TaskEvent,
  TasksData,
  MetricsSnapshot as MetricsSnapshotType,
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

// ── Metrics converters (gRPC → dashboard types) ──────────────

function grpcTaskMetricsToDashboard(m: GrpcTaskMetrics): MetricsSnapshotType["task"] {
  return {
    avg_duration_ms: m.avgDurationMs,
    p50_duration_ms: m.p50DurationMs,
    p95_duration_ms: m.p95DurationMs,
    p99_duration_ms: m.p99DurationMs,
    retry_rate: m.retryRate,
    slow_tasks_count: m.slowTasksCount,
    total_completed: m.totalCompleted,
    total_failed: m.totalFailed,
    success_rate: m.successRate,
  };
}

function grpcWorkerMetricsToDashboard(m: GrpcWorkerMetrics): MetricsSnapshotType["worker"] {
  const toolCalls: Record<string, number> = {};
  for (const [k, v] of Object.entries(m.perWorkerToolCalls)) toolCalls[k] = v;
  const subtaskCounts: Record<string, number> = {};
  for (const [k, v] of Object.entries(m.perWorkerSubtaskCount)) subtaskCounts[k] = v;
  return {
    avg_heartbeat_age_seconds: m.avgHeartbeatAgeSeconds,
    per_worker_tool_calls: toolCalls,
    per_worker_subtask_count: subtaskCounts,
    cluster_load_pct: m.clusterLoadPct,
  };
}

function grpcEventMetricsToDashboard(m: GrpcEventMetrics): MetricsSnapshotType["event"] {
  const counts: Record<string, number> = {};
  for (const [k, v] of Object.entries(m.eventTypeCounts)) counts[k] = v;
  return {
    events_per_minute: m.eventsPerMinute,
    error_spike: m.errorSpike,
    event_type_counts: counts,
  };
}

function grpcSystemMetricsToDashboard(m: GrpcSystemMetrics): MetricsSnapshotType["system"] {
  return {
    uptime_seconds: Number(m.uptimeSeconds),
    rate_limiter_remaining_ratio: m.rateLimiterRemainingRatio,
    cluster_utilization_pct: m.clusterUtilizationPct,
  };
}

function grpcMetricsToDashboard(m: GrpcMetricsSnapshot): MetricsSnapshotType {
  return {
    task: m.task ? grpcTaskMetricsToDashboard(m.task) : {
      avg_duration_ms: 0, p50_duration_ms: 0, p95_duration_ms: 0, p99_duration_ms: 0,
      retry_rate: 0, slow_tasks_count: 0, total_completed: 0, total_failed: 0, success_rate: 0,
    },
    worker: m.worker ? grpcWorkerMetricsToDashboard(m.worker) : {
      avg_heartbeat_age_seconds: 0, per_worker_tool_calls: {}, per_worker_subtask_count: {}, cluster_load_pct: 0,
    },
    event: m.event ? grpcEventMetricsToDashboard(m.event) : {
      events_per_minute: 0, error_spike: false, event_type_counts: {},
    },
    system: m.system ? grpcSystemMetricsToDashboard(m.system) : {
      uptime_seconds: 0, rate_limiter_remaining_ratio: 1.0, cluster_utilization_pct: 0,
    },
    trend: m.trend.map((s: GrpcMetricsSample) => ({
      timestamp: Number(s.timestamp),
      events_per_minute: s.eventsPerMinute,
      avg_duration_ms: s.avgDurationMs,
      error_rate: s.errorRate,
      cluster_utilization: s.clusterUtilization,
    })),
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
    events?: DashboardEvent[];
    metrics?: MetricsSnapshotType;
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
  // D1: connectionStateRef removed — dead write (never read); was flagged by react-hooks/refs.
  const abortRef = useRef<AbortController | null>(null);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const optsRef = useRef(opts);
  // eslint-disable-next-line react-hooks/refs -- stable-callback ref-mirror: synchronous to avoid one-frame race in reconnect timers
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
      // ponytail: use `delay` (the same exponential backoff as the gRPC path),
      // not a fixed 1000ms. SSE failing too (e.g. gRPC-only deploy with no
      // /dashboard/api/stream) would otherwise retry every 1s forever —
      // retryCount keeps climbing so delay grows up to MAX_RETRY_INTERVAL.
      retryTimerRef.current = setTimeout(() => {
        if (optsRef.current.enabled) {
          connectSseRef.current();
        }
      }, delay);
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
      } catch (e) { console.warn("[Dashboard SSE] task_event parse error:", e); }
    });

    sse.addEventListener("update", (e) => {
      try {
        const snapshot = JSON.parse(e.data);
        const converted: {
          health?: HealthData;
          workers?: WorkersData;
          scheduler?: SchedulerData;
          events?: DashboardEvent[];
          metrics?: MetricsSnapshotType;
        } = {};
        if (snapshot.health?.available) converted.health = snapshot.health;
        if (snapshot.workers?.available) converted.workers = snapshot.workers;
        if (snapshot.scheduler?.available) converted.scheduler = snapshot.scheduler;
        if (snapshot.metrics) converted.metrics = snapshot.metrics;
        if (snapshot.tasks) {
          // Merge task list from SSE snapshot
          if (optsRef.current.mergeGrpcTasks) {
            optsRef.current.mergeGrpcTasks(snapshot.tasks as TasksData);
          }
        }
        optsRef.current.onSnapshot?.(converted);
      } catch (e) { console.warn("[Dashboard SSE] snapshot parse error:", e); }
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

  // eslint-disable-next-line react-hooks/refs -- stable-callback ref-mirror: synchronous to avoid one-frame race in reconnect timers
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
            events?: DashboardEvent[];
            metrics?: MetricsSnapshotType;
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
          if (snapshot.recentEvents.length > 0) {
            converted.events = snapshot.recentEvents.map(grpcEventProtoToDashboardEvent);
          }
          if (snapshot.metrics) {
            converted.metrics = grpcMetricsToDashboard(snapshot.metrics);
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
  }, [clearRetryTimer, scheduleReconnect, getTransport]);

  // eslint-disable-next-line react-hooks/refs -- stable-callback ref-mirror: synchronous to avoid one-frame race in reconnect timers
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
  }, [getTransport]);

  const getSchedulerStatus = useCallback(async (): Promise<SchedulerData> => {
    const transport = getTransport();
    const client = createClient(DashboardService, transport);
    const resp = await client.getSchedulerStatus(create(GetSchedulerStatusRequestSchema, {}));
    return grpcSchedulerToDashboard(resp);
  }, [getTransport]);

  const triggerSchedulerJob = useCallback(async (jobId: string): Promise<{ success: boolean; error?: string }> => {
    const transport = getTransport();
    const client = createClient(DashboardService, transport);
    const resp = await client.triggerSchedulerJob(create(TriggerSchedulerJobRequestSchema, { jobId }));
    return { success: resp.success, error: resp.error ?? undefined };
  }, [getTransport]);

  const flushPendingTasks = useCallback(async (): Promise<{ success: boolean; pendingCount: number; executedCount: number; error?: string }> => {
    const transport = getTransport();
    const client = createClient(DashboardService, transport);
    const resp = await client.flushPendingTasks(create(FlushPendingTasksRequestSchema, {}));
    return { success: resp.success, pendingCount: resp.pendingCount, executedCount: resp.executedCount, error: resp.error ?? undefined };
  }, [getTransport]);

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
  }, [getTransport]);

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
    triggerSchedulerJob,
    flushPendingTasks,
    listEvents,
  };
}
