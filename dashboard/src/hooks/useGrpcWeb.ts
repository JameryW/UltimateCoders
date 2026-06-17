import { useEffect, useRef, useCallback, useState } from "react";
import { createClient } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import { TaskService } from "@/grpc/engine_pb";
import type { TaskEvent as GrpcTaskEvent } from "@/grpc/engine_pb";
import { create } from "@bufbuild/protobuf";
import { WatchTaskRequestSchema } from "@/grpc/engine_pb";
import type { TaskEvent } from "@/types/dashboard";

/** gRPC-Web server address — defaults to same-origin (tonic-web on :50051). */
const GRPC_WEB_ADDR =
  import.meta.env.VITE_GRPC_WEB_ADDR ?? "http://localhost:50051";

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
  const optsRef = useRef(opts);
  optsRef.current = opts;

  const connect = useCallback(() => {
    // Tear down any existing stream
    abortRef.current?.abort();
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

        for await (const event of stream) {
          if (ac.signal.aborted) break;
          const dashboardEvent = grpcEventToDashboardEvent(event);
          optsRef.current.onTaskEvent?.(dashboardEvent);
        }
      } catch (err: unknown) {
        if (ac.signal.aborted) return;
        console.error("[gRPC-Web] WatchTask stream error:", err);
        setConnectionState("error");
      }
    })();
  }, []);

  const disconnect = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setConnectionState("disconnected");
  }, []);

  useEffect(() => {
    connect();
    return disconnect;
  }, [connect, disconnect]);

  return { connectionState, connect, disconnect };
}
