import { useEffect, useRef, useCallback, useState } from "react";
import type { DashboardSnapshot, TaskEvent } from "@/types/dashboard";

interface SSEHandlers {
  onUpdate?: (snapshot: DashboardSnapshot) => void;
  onTaskEvent?: (event: TaskEvent) => void;
  /** Called when SSE reconnects — use to fetch missed data since last update. */
  onReconnect?: () => void;
}

/** Exponential backoff (ms) when EventSource exhausts its native reconnect. */
const RETRY_INTERVALS = [1000, 2000, 4000, 8000, 16000];
const MAX_RETRY = RETRY_INTERVALS.length;

/** Build the SSE URL with optional auth token as query parameter. */
function sseUrl(): string {
  const base = "/dashboard/api/stream";
  try {
    const token = localStorage.getItem("uc_dashboard_token");
    if (token) return `${base}?token=${encodeURIComponent(token)}`;
  } catch { /* ignore */ }
  return base;
}

export function useSSE(handlers: SSEHandlers) {
  const [connected, setConnected] = useState(false);
  const wasConnectedRef = useRef(false);
  const esRef = useRef<EventSource | null>(null);
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearRetryTimer = useCallback(() => {
    if (retryTimerRef.current !== null) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  const connect = useCallback(() => {
    if (esRef.current) esRef.current.close();
    clearRetryTimer();

    const es = new EventSource(sseUrl());
    esRef.current = es;

    es.addEventListener("update", (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data) as DashboardSnapshot;
        handlersRef.current.onUpdate?.(data);
        setConnected(true);
        retryCountRef.current = 0; // reset on successful data
      } catch (err) {
        console.error("Failed to parse SSE update:", err);
      }
    });

    es.addEventListener("task_event", (e: MessageEvent) => {
      try {
        const ev = JSON.parse(e.data) as TaskEvent;
        // Inject SSE event id for dedup — this comes from server's monotonic id field
        if (e.lastEventId) ev._sseId = e.lastEventId;
        handlersRef.current.onTaskEvent?.(ev);
        setConnected(true);
        retryCountRef.current = 0;
      } catch (err) {
        console.error("Failed to parse SSE task_event:", err);
      }
    });

    es.addEventListener("open", () => {
      const isReconnect = wasConnectedRef.current; // was connected before, now reconnecting
      setConnected(true);
      wasConnectedRef.current = true;
      retryCountRef.current = 0;
      // ponytail: on reconnect, notify caller to fetch missed data
      if (isReconnect) handlersRef.current.onReconnect?.();
    });

    // ponytail: leverage EventSource native reconnect; manual reconnect only when CLOSED
    es.addEventListener("error", () => {
      if (es.readyState === EventSource.CLOSED) {
        setConnected(false);
        wasConnectedRef.current = true; // was connected, now lost — marks as reconnect on next open
        // Native reconnect exhausted — schedule manual reconnect with backoff
        if (retryCountRef.current < MAX_RETRY) {
          const delay = RETRY_INTERVALS[retryCountRef.current];
          retryCountRef.current += 1;
          retryTimerRef.current = setTimeout(connect, delay);
        }
      } else {
        // CONNECTING = browser is retrying natively, just mark disconnected
        setConnected(false);
        wasConnectedRef.current = true; // will be reconnect when open fires
        setConnected(false);
      }
    });
  }, [clearRetryTimer]);

  const disconnect = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    clearRetryTimer();
    retryCountRef.current = 0;
    wasConnectedRef.current = false;
    setConnected(false);
  }, [clearRetryTimer]);

  useEffect(() => {
    connect();
    return disconnect;
  }, [connect, disconnect]);

  return { connected, reconnect: connect, disconnect };
}
