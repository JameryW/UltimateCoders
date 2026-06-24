import { useEffect, useRef, useCallback, useState } from "react";
import { createClient } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import type { Interceptor } from "@connectrpc/connect";
import { TaskService, EngineService } from "@/grpc/engine_pb";
import type { TaskEvent as GrpcTaskEvent } from "@/grpc/engine_pb";
import { create } from "@bufbuild/protobuf";
import { WatchTaskRequestSchema, SubmitTaskRequestSchema, HealthRequestSchema, ListTasksRequestSchema, PauseTaskRequestSchema, ResumeTaskRequestSchema } from "@/grpc/engine_pb";
import type { TaskEvent } from "@/types/dashboard";

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

/** Exponential backoff intervals (ms) for reconnection.
 *  No upper limit — keeps retrying indefinitely with capped delay. */
const RETRY_INTERVALS = [1000, 2000, 4000, 8000, 16000, 30000, 60000];
const MAX_RETRY_INTERVAL = 60000;

interface UseGrpcWebOptions {
  onTaskEvent?: (event: TaskEvent) => void;
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

export interface GrpcSubmitResult {
  success: boolean;
  taskId: string;
  status: string;
  subtaskCount: number;
  subtasks: Array<{ id: string; description: string; status: string; dependsOn: string[] }>;
}

/** #2: Normalize a gRPC timestamp to ISO string.
 *  Handles: bigint microseconds (>1e15), bigint milliseconds, ISO strings, numeric strings. */
function normalizeTimestamp(ts: string | bigint | number): string {
  // Already an ISO-like string
  if (typeof ts === "string") {
    // If it looks like a numeric string (microseconds or milliseconds)
    const asNum = Number(ts);
    if (!isNaN(asNum) && asNum > 1e15) {
      // Microseconds -> milliseconds -> ISO
      return new Date(asNum / 1000).toISOString();
    }
    if (!isNaN(asNum) && asNum > 1e12) {
      // Milliseconds -> ISO
      return new Date(asNum).toISOString();
    }
    // Already ISO/RFC3339
    return ts;
  }
  // BigInt -- could be microseconds or milliseconds
  if (typeof ts === "bigint") {
    // > 1e15 -> microseconds; divide to milliseconds
    if (ts > 1_000_000_000_000_000n) {
      return new Date(Number(ts / 1000n)).toISOString();
    }
    // Otherwise assume milliseconds
    return new Date(Number(ts)).toISOString();
  }
  // Number -- could be microseconds or milliseconds
  if (typeof ts === "number") {
    if (ts > 1e15) return new Date(ts / 1000).toISOString();
    return new Date(ts).toISOString();
  }
  return String(ts);
}

/** #12: Safe bigint-to-ISO conversion for gRPC timestamps.
 *  gRPC timestamps may be microseconds or milliseconds. This function
 *  handles both cases and checks against MAX_SAFE_INTEGER. */
function bigintToISO(ts: bigint): string {
  // If > 1e15, treat as microseconds -> divide by 1000 to get milliseconds
  if (ts > 1_000_000_000_000_000n) {
    const ms = ts / 1000n;
    // Check if result fits safely in Number
    if (ms <= Number.MAX_SAFE_INTEGER) {
      return new Date(Number(ms)).toISOString();
    }
    // For extremely large values, use remaining microseconds calculation
    const seconds = Number(ts / 1_000_000n);
    const micros = Number(ts % 1_000_000n);
    return new Date(seconds * 1000 + Math.floor(micros / 1000)).toISOString();
  }
  // Milliseconds range
  if (ts <= Number.MAX_SAFE_INTEGER) {
    return new Date(Number(ts)).toISOString();
  }
  // Fallback: divide to seconds
  const seconds = Number(ts / 1000n);
  return new Date(seconds * 1000).toISOString();
}

/** Convert gRPC TaskEvent to dashboard TaskEvent.
 *  gRPC proto data is map<string,string> -- values that look like JSON
 *  arrays/objects are parsed, numeric strings are converted, others kept as-is. */
function grpcEventToDashboardEvent(ev: GrpcTaskEvent): TaskEvent {
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
  connectionStateRef.current = connectionState;
  // #9: Track gRPC exhaustion state for stop-reconnect button
  const [grpcExhausted, setGrpcExhausted] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const optsRef = useRef(opts);
  optsRef.current = opts;
  // Ref breaks connect<->scheduleReconnect cycle
  const connectRef = useRef<() => void>(() => {});

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
        connectRef.current();
      }
    }, delay);
  }, []);

  const connect = useCallback(() => {
    // Tear down any existing stream
    abortRef.current?.abort();
    clearRetryTimer();
    // ponytail: reset retry count so manual reconnect always works even after exhaustion
    retryCountRef.current = 0;
    setGrpcExhausted(false);
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
        const stream = client.watchTask(req, { signal: ac.signal });
        setConnectionState("connected");
        retryCountRef.current = 0; // reset on successful connect
        setGrpcExhausted(false);

        for await (const event of stream) {
          if (ac.signal.aborted) break;

          // Handle sync_required: server tells us we missed events
          if (event.type === "sync_required") {
            const reason = (event.data as Record<string, string>)?.reason ?? "unknown";
            const skipped = Number((event.data as Record<string, string>)?.skipped ?? 0);
            console.warn(`[gRPC-Web] sync_required: ${reason}, ${skipped} events missed — re-syncing`);
            optsRef.current.onSyncRequired?.(reason, skipped);
            continue;
          }

          const dashboardEvent = grpcEventToDashboardEvent(event);
          optsRef.current.onTaskEvent?.(dashboardEvent);
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
  }, [clearRetryTimer, scheduleReconnect]);

  // Keep ref in sync so scheduleReconnect always calls the latest connect
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
          status: resp.status,
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
          throw new Error("gRPC submitTask timed out after 30s");
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
  }, []);

  /** Fetch task list via gRPC-Web. Returns TasksData-compatible structure. */
  const listTasks = useCallback(async () => {
    const transport = getTransport();
    const client = createClient(TaskService, transport);
    const resp = await client.listTasks(create(ListTasksRequestSchema, {}));
    return {
      available: resp.available,
      tasks: resp.tasks.map((t) => ({
        id: t.id,
        description: t.description,
        status: t.status,
        project_id: t.projectId,
        subtask_count: t.subtaskCount,
        subtasks: t.subtasks.map((s) => ({
          id: s.id,
          description: s.description,
          status: s.status,
          depends_on: [...s.dependsOn],
          assigned_worker: s.assignedWorker ?? undefined,
        })),
        // #12: Safe bigint conversion using bigintToISO helper
        created_at: bigintToISO(t.createdAt),
        updated_at: bigintToISO(t.updatedAt),
      })),
      total: resp.total,
      status_counts: resp.statusCounts as Record<string, number>,
      // ponytail: derive pending_task_count from status_counts instead of hardcoding 0
      pending_task_count: (resp.statusCounts as Record<string, number>)["pending"] ?? (resp.statusCounts as Record<string, number>)["submitted"] ?? 0,
    };
  }, []);

  /** Pause a running task via gRPC-Web. */
  const pauseTask = useCallback(async (taskId: string) => {
    const transport = getTransport();
    const client = createClient(TaskService, transport);
    const resp = await client.pauseTask(create(PauseTaskRequestSchema, { taskId }));
    return { success: resp.success, taskId: resp.taskId, status: resp.status, error: resp.error ?? undefined };
  }, []);

  /** Resume a paused task via gRPC-Web. */
  const resumeTask = useCallback(async (taskId: string) => {
    const transport = getTransport();
    const client = createClient(TaskService, transport);
    const resp = await client.resumeTask(create(ResumeTaskRequestSchema, { taskId }));
    return { success: resp.success, taskId: resp.taskId, status: resp.status, error: resp.error ?? undefined };
  }, []);

  useEffect(() => {
    connect();
    return disconnect;
  }, [connect, disconnect]);

  return { connectionState, grpcExhausted, connect, disconnect, submitTask, healthCheck, listTasks, pauseTask, resumeTask };
}
