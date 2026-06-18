import { useEffect, useRef, useCallback, useState } from "react";
import { createClient } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import { TaskService, EngineService } from "@/grpc/engine_pb";
import type { TaskEvent as GrpcTaskEvent } from "@/grpc/engine_pb";
import { create } from "@bufbuild/protobuf";
import { WatchTaskRequestSchema, SubmitTaskRequestSchema, HealthRequestSchema, ListTasksRequestSchema, PauseTaskRequestSchema, ResumeTaskRequestSchema } from "@/grpc/engine_pb";
import type { TaskEvent } from "@/types/dashboard";

/** gRPC-Web server address -- empty = same-origin (Vite proxy in dev, reverse proxy in prod). */
const GRPC_WEB_ADDR =
  import.meta.env.VITE_GRPC_WEB_ADDR ?? "";

// ponytail: module-level shared transport — single HTTP/2 connection reused by
// useGrpcWeb, SearchPanel, and any future gRPC-Web consumer.
let _sharedTransport: ReturnType<typeof createGrpcWebTransport> | null = null;
export function getSharedTransport() {
  if (!_sharedTransport) {
    _sharedTransport = createGrpcWebTransport({ baseUrl: GRPC_WEB_ADDR });
  }
  return _sharedTransport;
}

/** Exponential backoff intervals (ms) for reconnection. */
const RETRY_INTERVALS = [1000, 2000, 4000, 8000, 16000];
const MAX_RETRY = RETRY_INTERVALS.length;

interface UseGrpcWebOptions {
  onTaskEvent?: (event: TaskEvent) => void;
  enabled?: boolean;
}

export type GrpcConnectionState =
  | "disconnected"
  | "connecting"
  | "connected"
  | "error"
  | "exhausted"; // auto-retry exhausted, user must manually reconnect

export interface GrpcSubmitResult {
  success: boolean;
  taskId: string;
  status: string;
  subtaskCount: number;
  subtasks: Array<{ id: string; description: string; status: string; dependsOn: string[] }>;
}

/** Convert gRPC TaskEvent to dashboard TaskEvent.
 *  gRPC proto data is map<string,string> — values that look like JSON
 *  arrays/objects are parsed, others kept as-is. */
function grpcEventToDashboardEvent(ev: GrpcTaskEvent): TaskEvent {
  const data: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(ev.data)) {
    // ponytail: try JSON parse for structured values (subtasks, depends_on, etc.)
    if (value.startsWith("[") || value.startsWith("{")) {
      try { data[key] = JSON.parse(value); } catch { data[key] = value; }
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

export function useGrpcWeb(opts: UseGrpcWebOptions) {
  const [connectionState, setConnectionState] =
    useState<GrpcConnectionState>("disconnected");
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
    if (retryCountRef.current >= MAX_RETRY) {
      console.warn("[gRPC-Web] Max retries reached, entering exhausted state");
      setConnectionState("exhausted");
      return;
    }
    const delay = RETRY_INTERVALS[retryCountRef.current] ?? 16000;
    retryCountRef.current += 1;
    console.log(`[gRPC-Web] Reconnecting in ${delay}ms (attempt ${retryCountRef.current}/${MAX_RETRY})`);
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

        for await (const event of stream) {
          if (ac.signal.aborted) break;
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
    setConnectionState("disconnected");
  }, [clearRetryTimer]);

  const submitTask = useCallback(
    async (description: string, projectId: string = ""): Promise<GrpcSubmitResult> => {
      // ponytail: fail fast if not connected — prevents unhandled rejections
      if (connectionState !== "connected") {
        throw new Error(`gRPC-Web not connected (state: ${connectionState})`);
      }
      const transport = getTransport();
      const client = createClient(TaskService, transport);
      const req = create(SubmitTaskRequestSchema, { description, projectId });
      const resp = await client.submitTask(req);
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
        })),
      };
    },
    [connectionState],
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
        })),
        // ponytail: divide bigint first to avoid Number precision loss on large values
        created_at: new Date(Number(t.createdAt / 1000n) * 1000).toISOString(),
        updated_at: new Date(Number(t.updatedAt / 1000n) * 1000).toISOString(),
      })),
      total: resp.total,
      status_counts: resp.statusCounts as Record<string, number>,
      pending_task_count: 0,
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

  return { connectionState, connect, disconnect, submitTask, healthCheck, listTasks, pauseTask, resumeTask };
}
