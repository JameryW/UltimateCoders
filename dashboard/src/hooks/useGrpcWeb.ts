import { useEffect, useRef, useCallback, useState } from "react";
import { createClient } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import { TaskService } from "@/grpc/engine_pb";
import type { TaskEvent as GrpcTaskEvent } from "@/grpc/engine_pb";
import { create } from "@bufbuild/protobuf";
import { WatchTaskRequestSchema, SubmitTaskRequestSchema } from "@/grpc/engine_pb";
import type { TaskEvent } from "@/types/dashboard";

/** gRPC-Web server address — defaults to same-origin (tonic-web on :50051). */
const GRPC_WEB_ADDR =
  import.meta.env.VITE_GRPC_WEB_ADDR ?? "http://localhost:50051";

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
  | "error";

function grpcEventToDashboardEvent(ev: GrpcTaskEvent): TaskEvent {
  return {
    timestamp: ev.timestamp,
    type: ev.type,
    task_id: ev.taskId,
    subtask_id: ev.subtaskId ?? undefined,
    data: { ...ev.data },
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

  const clearRetryTimer = useCallback(() => {
    if (retryTimerRef.current !== null) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  const connect = useCallback(() => {
    // Tear down any existing stream
    abortRef.current?.abort();
    clearRetryTimer();
    const ac = new AbortController();
    abortRef.current = ac;

    if (!optsRef.current.enabled) {
      setConnectionState("disconnected");
      return;
    }

    setConnectionState("connecting");

    const transport = createGrpcWebTransport({
      baseUrl: GRPC_WEB_ADDR,
    });

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

        // Stream ended normally (server closed) — reconnect
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
  }, [clearRetryTimer]);

  const scheduleReconnect = useCallback(() => {
    if (retryCountRef.current >= MAX_RETRY) {
      console.warn("[gRPC-Web] Max retries reached, giving up");
      return;
    }
    const delay = RETRY_INTERVALS[retryCountRef.current] ?? 16000;
    retryCountRef.current += 1;
    console.log(`[gRPC-Web] Reconnecting in ${delay}ms (attempt ${retryCountRef.current}/${MAX_RETRY})`);
    retryTimerRef.current = setTimeout(() => {
      if (optsRef.current.enabled) {
        connect();
      }
    }, delay);
  }, [connect]);

  const disconnect = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    clearRetryTimer();
    retryCountRef.current = 0;
    setConnectionState("disconnected");
  }, [clearRetryTimer]);

  const submitTask = useCallback(
    async (description: string, projectId: string = "") => {
      const transport = createGrpcWebTransport({ baseUrl: GRPC_WEB_ADDR });
      const client = createClient(TaskService, transport);
      const req = create(SubmitTaskRequestSchema, { description, projectId });
      const resp = await client.submitTask(req);
      return { success: resp.success, taskId: resp.taskId, status: resp.status };
    },
    [],
  );

  useEffect(() => {
    connect();
    return disconnect;
  }, [connect, disconnect]);

  return { connectionState, connect, disconnect, submitTask };
}
