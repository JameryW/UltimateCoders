import { useEffect, useRef, useCallback, useState } from "react";
import type { DashboardSnapshot, TaskEvent } from "@/types/dashboard";

interface SSEHandlers {
  onUpdate?: (snapshot: DashboardSnapshot) => void;
  onTaskEvent?: (event: TaskEvent) => void;
}

export function useSSE(handlers: SSEHandlers) {
  const [connected, setConnected] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  const connect = useCallback(() => {
    if (esRef.current) esRef.current.close();

    const es = new EventSource("/dashboard/api/stream");
    esRef.current = es;

    es.addEventListener("update", (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data) as DashboardSnapshot;
        handlersRef.current.onUpdate?.(data);
        setConnected(true);
      } catch (err) {
        console.error("Failed to parse SSE update:", err);
      }
    });

    es.addEventListener("task_event", (e: MessageEvent) => {
      try {
        const ev = JSON.parse(e.data) as TaskEvent;
        handlersRef.current.onTaskEvent?.(ev);
        setConnected(true);
      } catch (err) {
        console.error("Failed to parse SSE task_event:", err);
      }
    });

    es.addEventListener("error", () => setConnected(false));
    es.addEventListener("open", () => setConnected(true));
  }, []);

  const disconnect = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    setConnected(false);
  }, []);

  useEffect(() => {
    connect();
    return disconnect;
  }, [connect, disconnect]);

  return { connected, reconnect: connect, disconnect };
}
