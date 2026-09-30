import { useEffect, useRef, useCallback, useState } from "react";
import { createClient } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import type { Interceptor } from "@connectrpc/connect";
import { TaskService, EngineService } from "@/grpc/engine_pb";
import type { TaskEvent as GrpcTaskEvent } from "@/grpc/engine_pb";
import { create } from "@bufbuild/protobuf";
import { WatchTaskRequestSchema, SubmitTaskRequestSchema, HealthRequestSchema, ListTasksRequestSchema, PauseTaskRequestSchema, ResumeTaskRequestSchema, CancelTaskRequestSchema } from "@/grpc/engine_pb";
import type { UcSubmitResult, UcTaskActionResult } from "@/lib/ucCommands";
import { normalizeGrpcStatus } from "@/lib/grpcStatus";
import { eventTimestampToISO, taskTimestampToISO } from "@/lib/grpcTimestamp";
import type { ListTasksResponse } from "@/grpc/engine_pb";
import type { TasksData } from "@/types/dashboard";

type GrpcSubmitResult = UcSubmitResult;
type GrpcTaskActionResult = UcTaskActionResult;
export type { GrpcSubmitResult, GrpcTaskActionResult };

interface TuiTaskEvent {
  timestamp: string;
  type: string;
  task_id: string;
  subtask_id?: string;
  data: Record<string, unknown>;
}

/** gRPC-Web server address -- empty = same-origin (Vite proxy in dev, reverse proxy in prod). */
const GRPC_WEB_ADDR =
  import.meta.env.VITE_GRPC_WEB_ADDR ?? "";

// ponytail: auth interceptor — reads token from localStorage, attaches as authorization header
const authInterceptor: Interceptor = (next) => async (req) => {
  const token = localStorage.getItem("uc_dashboard_token");
  if (token) {
    req.header.set("authorization", `Bearer ${token}`);
  }
  return next(req);
};

// ponytail: module-level shared transport — single HTTP/2 connection reused by
// useGrpcWeb, SearchPanel, and any future gRPC-Web consumer.
let _sharedTransport: ReturnType<typeof createGrpcWebTransport> | null = null;
export function getSharedTransport() {
  if (!_sharedTransport) {
    _sharedTransport = createGrpcWebTransport({
      baseUrl: GRPC_WEB_ADDR,
      interceptors: [authInterceptor],
    });
  }
  return _sharedTransport;
}

/** ponytail: F74 — shared 30s timeout for unary calls. Previously only
 * submitTask had one: a server that accepts TCP but stalls left
 * pause/resume/cancel/listTasks/search promises unsettled forever —
 * optimistic UI state never reverted, SearchPanel spun "Searching…"
 * eternally. AbortError becomes a descriptive timeout error. */
export async function unaryWithTimeout<T>(
  call: (signal: AbortSignal) => Promise<T>,
  what: string,
  timeoutMs = 30_000,
): Promise<T> {
  const ac = new AbortController();
  const timeoutId = setTimeout(() => ac.abort(), timeoutMs);
  try {
    return await call(ac.signal);
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw Object.assign(
        new Error(`${what} timed out after ${Math.round(timeoutMs / 1000)}s`),
        { cause: err },
      );
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

/** Exponential backoff intervals (ms) for reconnection.
 *  No upper limit — keeps retrying indefinitely with capped delay. */
const RETRY_INTERVALS = [1000, 2000, 4000, 8000, 16000, 30000, 60000];
const MAX_RETRY_INTERVAL = 60000;

interface UseGrpcWebOptions {
  onTaskEvent?: (event: TuiTaskEvent) => void;
  /** Called when the server signals that events were missed and client should re-sync. */
  onSyncRequired?: (reason: string, skipped: number) => void;
  enabled?: boolean;
}

export type GrpcConnectionState =
  | "disconnected"
  | "connecting"
  | "connected"
  | "error"
  | "reconnecting";

/** Normalize event timestamps from every transport. */
function normalizeTimestamp(ts: string | bigint | number): string {
  return eventTimestampToISO(ts);
}

export function grpcTasksToDashboard(resp: ListTasksResponse): TasksData {
  const statusCounts = Object.fromEntries(
    Object.entries(resp.statusCounts).map(([status, count]) => [normalizeGrpcStatus(status), count]),
  );
  return {
    available: resp.available,
    tasks: resp.tasks.map((t) => ({
      id: t.id,
      description: t.description,
      status: normalizeGrpcStatus(t.status),
      project_id: t.projectId,
      subtask_count: t.subtaskCount,
      subtasks: t.subtasks.map((s) => ({
        id: s.id,
        description: s.description,
        status: normalizeGrpcStatus(s.status),
        depends_on: [...s.dependsOn],
        assigned_worker: s.assignedWorker ?? undefined,
        result: s.result ?? undefined,
      })),
      created_at: taskTimestampToISO(t.createdAt),
      updated_at: taskTimestampToISO(t.updatedAt),
    })),
    total: resp.total,
    status_counts: statusCounts,
    // ponytail: derive pending_task_count from status_counts instead of hardcoding 0
    pending_task_count: statusCounts.pending ?? statusCounts.submitted ?? 0,
  };
}

/** Convert a gRPC TaskEvent to the event shape used by the Dashboard.
 *  gRPC proto data is map<string,string> -- values that look like JSON
 *  arrays/objects are parsed, numeric strings are converted, others kept as-is. */
function grpcEventToTuiEvent(ev: GrpcTaskEvent): TuiTaskEvent {
  const data: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(ev.data)) {
    // ponytail: try JSON parse for structured values (subtasks, depends_on, etc.)
    if (value.startsWith("[") || value.startsWith("{")) {
      try { data[key] = JSON.parse(value); } catch { data[key] = value; }
    } else if (/^-?\d+(\.\d+)?$/.test(value)) {
      // Numeric string -> convert to number for consistency with SSE path
      data[key] = Number(value);
    } else if (value === "true" || value === "false") {
      data[key] = value === "true";
    } else {
      data[key] = value;
    }
  }
  return {
    timestamp: normalizeTimestamp(ev.timestamp),
    type: ev.type,
    task_id: ev.taskId,
    subtask_id: ev.subtaskId ?? undefined,
    data,
  };
}

export function useGrpcWeb(opts: UseGrpcWebOptions) {
  const [connectionState, setConnectionState] =
    useState<GrpcConnectionState>("disconnected");
  // #4: Ref to track real-time connection state, avoiding stale closure
  const connectionStateRef = useRef<GrpcConnectionState>(connectionState);
  // eslint-disable-next-line react-hooks/refs -- stable-callback ref-mirror: submitTask reads synchronously to check real-time state
  connectionStateRef.current = connectionState;
  // #9: Track gRPC exhaustion state for stop-reconnect button
  const [grpcExhausted, setGrpcExhausted] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const optsRef = useRef(opts);
  // eslint-disable-next-line react-hooks/refs -- stable-callback ref-mirror: synchronous to avoid one-frame race in reconnect timers
  optsRef.current = opts;
  // Ref breaks connect<->scheduleReconnect cycle
  const connectRef = useRef<(resetRetries?: boolean) => void>(() => {});

  // ponytail: use shared transport (single HTTP/2 connection for all gRPC-Web)
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
    // #9: Mark exhausted when we've exceeded all defined retry intervals
    if (retryCountRef.current > RETRY_INTERVALS.length) {
      setGrpcExhausted(true);
    }
    if (import.meta.env.DEV) console.log(`[gRPC-Web] Reconnecting in ${delay}ms (attempt ${retryCountRef.current})`);
    setConnectionState("reconnecting");
    retryTimerRef.current = setTimeout(() => {
      if (optsRef.current.enabled) {
        connectRef.current(false);
      }
    }, delay);
  }, []);

  const connect = useCallback((resetRetries = true) => {
    // Tear down any existing stream
    abortRef.current?.abort();
    clearRetryTimer();
    // ponytail: reset retry count so manual reconnect always works even after exhaustion
    if (resetRetries) {
      retryCountRef.current = 0;
      setGrpcExhausted(false);
    }
    const ac = new AbortController();
    abortRef.current = ac;

    if (!optsRef.current.enabled) {
      setConnectionState("disconnected");
      return;
    }

    setConnectionState("connecting");

    const transport = getTransport();
    const client = createClient(TaskService, transport);
    const req = create(WatchTaskRequestSchema, { taskId: "" }); // empty = watch all

    (async () => {
      try {
        const stream = client.watchTask(req, {
          signal: ac.signal,
          onHeader: () => {
            if (ac.signal.aborted) return;
            setConnectionState("connected");
            setGrpcExhausted(false);
          },
        });

        for await (const event of stream) {
          if (ac.signal.aborted) break;
          retryCountRef.current = 0;

          // Handle sync_required: server tells us we missed events
          if (event.type === "sync_required") {
            const reason = (event.data as Record<string, string>)?.reason ?? "unknown";
            const skipped = Number((event.data as Record<string, string>)?.skipped ?? 0);
            console.warn(`[gRPC-Web] sync_required: ${reason}, ${skipped} events missed — re-syncing`);
            optsRef.current.onSyncRequired?.(reason, skipped);
            continue;
          }

          const tuiEvent = grpcEventToTuiEvent(event);
          optsRef.current.onTaskEvent?.(tuiEvent);
        }

        // Stream ended normally (server closed) -- reconnect
        if (!ac.signal.aborted) {
          setConnectionState("error");
          scheduleReconnect();
        }
      } catch (err: unknown) {
        if (ac.signal.aborted) return;
        console.error("[gRPC-Web] WatchTask stream error:", err);
        setConnectionState("error");
        scheduleReconnect();
      }
    })();
  }, [clearRetryTimer, scheduleReconnect, getTransport]);

  // Keep ref in sync so scheduleReconnect always calls the latest connect
  // eslint-disable-next-line react-hooks/refs -- stable-callback ref-mirror: synchronous to avoid one-frame race in reconnect timers
  connectRef.current = connect;

  const disconnect = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    clearRetryTimer();
    retryCountRef.current = 0;
    setGrpcExhausted(false);
    setConnectionState("disconnected");
  }, [clearRetryTimer]);

  const submitTask = useCallback(
    async (description: string, projectId: string = ""): Promise<GrpcSubmitResult> => {
      // #4: Use ref to check real-time state instead of stale closure
      if (connectionStateRef.current === "disconnected") {
        throw new Error("gRPC-Web disconnected — enable connection first");
      }
      // ponytail: if reconnecting, attempt the call anyway — the transport
      // will queue/retry internally. Only fail hard if truly disconnected.
      const transport = getTransport();
      const client = createClient(TaskService, transport);
      const req = create(SubmitTaskRequestSchema, { description, projectId });
      // #10: Add 30s timeout via AbortController to prevent indefinite hang
      const ac = new AbortController();
      const timeoutId = setTimeout(() => ac.abort(), 30_000);
      try {
        const resp = await client.submitTask(req, { signal: ac.signal });
        return {
          success: resp.success,
          taskId: resp.taskId,
          status: normalizeGrpcStatus(resp.status),
          error: resp.error,
          subtaskCount: resp.subtaskCount,
          subtasks: resp.subtasks.map((s) => ({
            id: s.id,
            description: s.description,
            status: s.status,
            dependsOn: [...s.dependsOn],
            assignedWorker: s.assignedWorker ?? undefined,
          })),
        };
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") {
          throw Object.assign(new Error("gRPC submitTask timed out after 30s"), { cause: err });
        }
        throw err;
      } finally {
        clearTimeout(timeoutId);
      }
    },
    [getTransport],
  );

  const healthCheck = useCallback(async () => {
    const transport = getTransport();
    const client = createClient(EngineService, transport);
    const req = create(HealthRequestSchema, {});
    const resp = await client.health(req);
    return {
      status: resp.status,
      version: resp.version,
      uptimeSeconds: resp.uptimeSeconds,
      components: resp.components.map((c: { name: string; status: string; details?: string }) => ({
        name: c.name,
        status: c.status,
        details: c.details ?? undefined,
      })),
    };
  }, [getTransport]);

  /** Fetch task list via gRPC-Web. Returns TasksData-compatible structure. */
  const listTasks = useCallback(async () => {
    const transport = getTransport();
    const client = createClient(TaskService, transport);
    const req = create(ListTasksRequestSchema, {});
    const resp = await unaryWithTimeout((signal) => client.listTasks(req, { signal }), "listTasks");
    return grpcTasksToDashboard(resp);
  }, [getTransport]);

  /** Pause a running task via gRPC-Web. */
  const pauseTask = useCallback(async (taskId: string): Promise<GrpcTaskActionResult> => {
    const transport = getTransport();
    const client = createClient(TaskService, transport);
    const req = create(PauseTaskRequestSchema, { taskId });
    const resp = await unaryWithTimeout((signal) => client.pauseTask(req, { signal }), "pauseTask");
    return { success: resp.success, taskId: resp.taskId, status: resp.status, error: resp.error ?? undefined };
  }, [getTransport]);

  /** Resume a paused task via gRPC-Web. */
  const resumeTask = useCallback(async (taskId: string): Promise<GrpcTaskActionResult> => {
    const transport = getTransport();
    const client = createClient(TaskService, transport);
    const req = create(ResumeTaskRequestSchema, { taskId });
    const resp = await unaryWithTimeout((signal) => client.resumeTask(req, { signal }), "resumeTask");
    return { success: resp.success, taskId: resp.taskId, status: resp.status, error: resp.error ?? undefined };
  }, [getTransport]);

  /** Cancel a task via gRPC-Web. */
  const cancelTask = useCallback(async (taskId: string): Promise<GrpcTaskActionResult> => {
    const transport = getTransport();
    const client = createClient(TaskService, transport);
    const req = create(CancelTaskRequestSchema, { taskId });
    const resp = await unaryWithTimeout((signal) => client.cancelTask(req, { signal }), "cancelTask");
    return { success: resp.success, taskId: resp.taskId, status: resp.status, error: resp.error ?? undefined };
  }, [getTransport]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- canonical connect-on-mount pattern; setState cascades are expected and harmless
    connect();
    return disconnect;
  }, [connect, disconnect]);

  return { connectionState, grpcExhausted, connect, disconnect, submitTask, healthCheck, listTasks, pauseTask, resumeTask, cancelTask };
}
