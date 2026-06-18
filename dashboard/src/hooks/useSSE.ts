import { useEffect, useRef, useCallback, useState } from "react";
import type { DashboardSnapshot, TaskEvent } from "@/types/dashboard";

interface SSEHandlers {
  onUpdate?: (snapshot: DashboardSnapshot) => void;
  onTaskEvent?: (event: TaskEvent) => void;
  /** Called when SSE reconnects — use to fetch missed data since last update. */
  onReconnect?: () => void;
}

/** Exponential backoff (ms) when EventSource exhausts its native reconnect.
 *  No upper limit — keeps retrying indefinitely with capped delay. */
const RETRY_INTERVALS = [1000, 2000, 4000, 8000, 16000, 30000, 60000];
const MAX_RETRY_INTERVAL = 60000;

/** Build the SSE URL with optional auth token as query parameter.
 *
 * SECURITY NOTE (#9): The auth token is currently passed as a URL query parameter
 * (`?token=...`), which means it appears in server access logs, CDN logs, and
 * browser history. The preferred approach is a short-lived, HttpOnly,
 * SameSite=Strict cookie (e.g. `uc_dashboard_sse`) set by the server on login
 * and read server-side to authenticate SSE connections. This requires a backend
 * change and is tracked as a follow-up. For now, the query-param approach is
 * retained since no backend cookie endpoint exists yet.
 */
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
  /** #11: wasConnectedRef tracks whether we've ever received real data from the
   *  server. It is set true in data handlers (update/task_event) when actual
   *  messages arrive, NOT in the open handler or transient error handler.
   *  This prevents false-positive "reconnect" triggers on transient errors
   *  that never reached real connectivity. */
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
        wasConnectedRef.current = true; // #11: real data received — mark as connected
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
        wasConnectedRef.current = true; // #11: real data received — mark as connected
        retryCountRef.current = 0;
      } catch (err) {
        console.error("Failed to parse SSE task_event:", err);
      }
    });

    es.addEventListener("open", () => {
      const isReconnect = wasConnectedRef.current; // was connected before, now reconnecting
      setConnected(true);
      // #11: Do NOT set wasConnectedRef=true here on its own. It is only set true
      // when real data arrives (in update/task_event handlers) or when a CLOSED
      // error confirms we previously had data. This prevents false-positives from
      // transient CONNECTING errors that never reached real data exchange.
      retryCountRef.current = 0;
      // ponytail: on reconnect, notify caller to fetch missed data
      if (isReconnect) handlersRef.current.onReconnect?.();
    });

    // ponytail: leverage EventSource native reconnect; manual reconnect only when CLOSED
    es.addEventListener("error", () => {
      if (es.readyState === EventSource.CLOSED) {
        setConnected(false);
        // wasConnectedRef preserves its current value (true if data was ever received).
        // This correctly marks the next successful open as a reconnect.
        // Native reconnect exhausted — schedule manual reconnect with backoff
        // No upper limit — keeps retrying indefinitely (capped at 60s intervals)
        const delay = RETRY_INTERVALS[retryCountRef.current] ?? MAX_RETRY_INTERVAL;
        retryCountRef.current += 1;
        retryTimerRef.current = setTimeout(connect, delay);
      } else {
        // CONNECTING = browser is retrying natively, just mark disconnected
        // #2: Removed duplicate setConnected(false) — only call once.
        // #11: Do NOT set wasConnectedRef=true here; we haven't actually lost
        // a real connection, the browser is just retrying natively.
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
