import { useEffect, useRef, useMemo, useState } from "react";
import { useDashboard } from "@/hooks/useDashboard";
import { useSSE } from "@/hooks/useSSE";
import { useGrpcWeb } from "@/hooks/useGrpcWeb";
import { useAuth } from "@/hooks/useAuth";
import { useTheme } from "@/hooks/useTheme";
import { Header } from "@/components/layout/Header";
import { ConnectionIndicator } from "@/components/layout/ConnectionIndicator";
import { HealthPanel } from "@/components/panels/HealthPanel";
import { WorkersPanel } from "@/components/panels/WorkersPanel";
import { TasksPanel } from "@/components/panels/TasksPanel";
import { SchedulerPanel } from "@/components/panels/SchedulerPanel";
import { CircuitBreakerPanel } from "@/components/panels/CircuitBreakerPanel";
import { EventLogPanel } from "@/components/panels/EventLogPanel";
import { SearchPanel } from "@/components/panels/SearchPanel";
import { TaskTrendChart } from "@/components/charts/TaskTrendChart";
import { TaskSubmitForm } from "@/components/forms/TaskSubmitForm";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ToastContainer, showToast } from "@/components/ui/toast";
import { confirmAction } from "@/components/ui/confirm-dialog";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import * as api from "@/api/endpoints";
import type { TaskEvent, HealthData, DashboardSnapshot } from "@/types/dashboard";

function eventKey(ev: TaskEvent): string {
  // ponytail: exclude timestamp — SSE and gRPC-Web emit the same logical event
  // with slightly different timestamps, causing double-processing.
  // Use full data hash (truncated to 80 chars) so subtask_started vs subtask_completed
  // for the same subtask within 1s don't collide (they have different data fields).
  const dataHash = ev.type === "sync_required" ? "" : JSON.stringify(ev.data).slice(0, 80);
  return `${ev.task_id}:${ev.subtask_id ?? ""}:${ev.type}:${dataHash}`;
}

/** Login modal shown when auth is required but user is not authenticated. */
function LoginModal({ onLogin }: { onLogin: (password: string) => Promise<boolean> }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError("");
    const ok = await onLogin(password);
    if (!ok) {
      setError("Invalid password");
    }
    setSubmitting(false);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      role="dialog"
      aria-modal="true"
      aria-label="Dashboard Login"
      onKeyDown={(e) => {
        if (e.key === "Escape") return; // Cannot dismiss login modal
      }}
    >
      <form
        onSubmit={handleSubmit}
        className="bg-[var(--bg-surface)] border border-[var(--border-color)] rounded-xl p-6 max-w-sm w-[90%] shadow-xl"
      >
        <h2 className="text-lg font-semibold text-[var(--text-primary)] mb-2">Dashboard Login</h2>
        <p className="text-sm text-[var(--text-secondary)] mb-4">Enter the dashboard password to continue.</p>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          autoFocus
          className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm text-[var(--text-primary)] focus:border-blue-500 focus:outline-none mb-3"
        />
        {error && <p className="text-sm text-red-400 mb-2">{error}</p>}
        <button
          type="submit"
          disabled={submitting || !password}
          className="w-full px-4 py-2 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-500 disabled:opacity-50 transition-colors"
        >
          {submitting ? "Verifying..." : "Login"}
        </button>
        <p className="text-xs text-[var(--text-secondary)] mt-2 text-center">Login required to access dashboard</p>
      </form>
    </div>
  );
}

function App() {
  const auth = useAuth();
  const { theme, toggleTheme } = useTheme();
  const dashboard = useDashboard();

  // Dedup: prefer SSE event id (monotonic, server-assigned) for exact dedup.
  // Fallback to content key + 2s window for gRPC-Web events (no server id).
  const seenSseIds = useRef(new Set<string>());
  const seenContentKeys = useRef(new Map<string, number>());
  // #8: Counter ref for sync_required race condition
  const needsSyncCountRef = useRef(0);
  const dedupedHandleTaskEvent = (ev: TaskEvent) => {
    // SSE event id dedup — exact, no time window needed
    if (ev._sseId) {
      if (seenSseIds.current.has(ev._sseId)) return;
      seenSseIds.current.add(ev._sseId);
      // Prune: keep last 10000 SSE ids (monotonic, so old ones never match)
      if (seenSseIds.current.size > 10000) {
        const arr = Array.from(seenSseIds.current);
        seenSseIds.current = new Set(arr.slice(arr.length - 5000));
      }
    } else {
      // gRPC-Web: content key + 1s window fallback
      const key = eventKey(ev);
      const now = Date.now();
      const lastSeen = seenContentKeys.current.get(key);
      if (lastSeen !== undefined && now - lastSeen < 1000) return;
      seenContentKeys.current.set(key, now);
      if (seenContentKeys.current.size > 5000) {
        for (const [k, ts] of seenContentKeys.current) {
          if (now - ts > 60_000) seenContentKeys.current.delete(k);
        }
      }
    }
    dashboard.handleTaskEvent(ev);
  };

  // Track last update timestamp for Header display
  const [lastUpdate, setLastUpdate] = useState<string | undefined>();
  // Track newly submitted task for highlight + auto-scroll
  const [highlightTaskId, setHighlightTaskId] = useState<string | null>(null);

  const { connected, reconnect: sseReconnect } = useSSE({
    onUpdate: (snapshot: DashboardSnapshot) => {
      dashboard.handleSnapshot(snapshot);
      setLastUpdate(snapshot.timestamp ?? new Date().toISOString());
    },
    onTaskEvent: (ev: TaskEvent) => {
      dedupedHandleTaskEvent(ev);
      setLastUpdate(ev.timestamp);
    },
    onReconnect: () => {
      // ponytail: SSE reconnected — clear dedup caches since server may have
      // reset event id sequence, and fetch fresh data to fill gaps from disconnect period
      seenSseIds.current.clear();
      seenContentKeys.current.clear();
      dashboard.fetchInitial().then((errors) => {
        if (Object.keys(errors).length > 0) showToast(`Reconnect fetch partially failed`, "error");
      });
    },
  });

  const { connectionState: grpcState, grpcExhausted, submitTask: grpcSubmitTask, healthCheck, connect: grpcConnect, disconnect: grpcDisconnect, listTasks, pauseTask: grpcPauseTask, resumeTask: grpcResumeTask } = useGrpcWeb({
    onTaskEvent: dedupedHandleTaskEvent,
    onSyncRequired: (_reason: string, _skipped: number) => {
      // #8: Increment counter ref instead of boolean to avoid race condition
      needsSyncCountRef.current += 1;
      dashboard.setNeedsSync(true);
    },
    enabled: true,
  });

  // Loading state -- show spinner until initial data arrives
  const [loading, setLoading] = useState(true);

  // Fetch initial data once auth is confirmed (skip while auth is still checking
  // to avoid 401 errors that silently fail and leave the dashboard empty).
  const hasFetchedRef = useRef(false);
  // #11: Track whether we skipped tasks in fetchInitial (gRPC path expected to provide them)
  const skippedTasksRef = useRef(false);
  useEffect(() => {
    if (auth.isChecking || !auth.isAuthenticated || hasFetchedRef.current) return;
    hasFetchedRef.current = true;
    const skipTasks = grpcState === "connected";
    skippedTasksRef.current = skipTasks;
    dashboard.fetchInitial({ skipTasks }).then((errors) => {
      setLoading(false);
      if (Object.keys(errors).length > 0) showToast(`Some panels failed to load`, "error");
    });
  }, [auth.isChecking, auth.isAuthenticated, dashboard.fetchInitial, grpcState]);

  // #11: When tasks were skipped in fetchInitial (gRPC path expected to provide them),
  // but gRPC listTasks fails, fall back to REST getTasks so tasks panel isn't empty.
  useEffect(() => {
    if (!skippedTasksRef.current) return;
    if (grpcState !== "connected") return;
    if (dashboard.tasks.available && dashboard.tasks.tasks.length > 0) return;
    // gRPC connected but no tasks loaded — try REST fallback once
    skippedTasksRef.current = false; // prevent repeated attempts
    api.getTasks().then((data) => {
      if (data.available) dashboard.mergeGrpcTasks(data);
    }).catch(() => { /* ignore */ });
  }, [grpcState, dashboard.tasks.available, dashboard.tasks.tasks.length, dashboard.mergeGrpcTasks]);

  // When auth transitions to authenticated, reconnect SSE with the stored token.
  // The initial SSE connection (on mount) may have been made without a token,
  // causing 401 errors. Reconnecting picks up the token from localStorage.
  const prevAuthRef = useRef(false);
  useEffect(() => {
    if (auth.isAuthenticated && !prevAuthRef.current) {
      sseReconnect();
    }
    prevAuthRef.current = auth.isAuthenticated;
  }, [auth.isAuthenticated, sseReconnect]);

  useEffect(() => { dashboard.setConnected(connected); }, [connected, dashboard.setConnected]);

  // gRPC-Web fallback: when SSE unavailable but gRPC connected, fetch tasks via gRPC
  useEffect(() => {
    if (!connected && grpcState === "connected") {
      listTasks().then((data) => {
        if (data.available) dashboard.mergeGrpcTasks(data);
      }).catch(() => { /* ignore */ });
    }
  }, [connected, grpcState, listTasks, dashboard.mergeGrpcTasks]);

  // Handle sync_required from broadcast lag — re-sync via gRPC or REST
  // #8: Use counter ref to avoid race condition where multiple sync_required
  // events are swallowed by a single boolean. Increment on sync_required,
  // decrement after processing. Only process when counter > 0.
  useEffect(() => {
    if (needsSyncCountRef.current <= 0) return;
    needsSyncCountRef.current -= 1;
    if (grpcState === "connected") {
      listTasks().then((data) => {
        if (data.available) dashboard.mergeGrpcTasks(data);
      }).catch(() => { /* ignore */ });
    } else {
      // REST fallback: re-fetch tasks to fill gap
      api.getTasks().then((data) => {
        if (data.available) dashboard.mergeGrpcTasks(data);
      }).catch(() => { /* ignore */ });
    }
  }, [dashboard.needsSync, grpcState, listTasks, dashboard.mergeGrpcTasks]);

  // Periodic gRPC health check -- poll every 30s when connected
  const [grpcHealthComponents, setGrpcHealthComponents] = useState<{ name: string; status: string; details?: string }[]>([]);
  useEffect(() => {
    if (grpcState !== "connected") {
      setGrpcHealthComponents([]);
      return;
    }
    const poll = async () => {
      try {
        const h = await healthCheck();
        setGrpcHealthComponents(h.components);
        setLastUpdate(new Date().toISOString());
      } catch { /* ignore — next poll will retry */ }
    };
    poll();
    const timer = setInterval(poll, 30000);
    return () => clearInterval(timer);
  }, [grpcState, healthCheck]);

  // Merge gRPC-Web connection + gRPC health components into HealthData
  const healthWithGrpc = useMemo<HealthData>(() => {
    const grpcStatus = grpcState === "connected" ? "ok"
      : grpcState === "connecting" ? "degraded"
      : grpcState === "error" || grpcState === "reconnecting" ? "error"
      : "unavailable";
    const grpcComponent = { name: "gRPC-Web", status: grpcStatus };
    let components = [...dashboard.health.components];
    for (const gc of grpcHealthComponents) {
      const idx = components.findIndex((c) => c.name === gc.name);
      if (idx >= 0) components[idx] = gc;
      else components.push(gc);
    }
    const grpcIdx = components.findIndex((c) => c.name === "gRPC-Web");
    if (grpcIdx >= 0) components[grpcIdx] = grpcComponent;
    else components.push(grpcComponent);
    return { ...dashboard.health, components };
  }, [dashboard.health, grpcState, grpcHealthComponents]);

  // ── Hash routing ──────────────────────────────────────────────
  useEffect(() => {
    const onHashChange = () => {
      const hash = window.location.hash.replace("#", "");
      if (hash) {
        const el = document.getElementById(hash);
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      }
    };
    window.addEventListener("hashchange", onHashChange);
    // Scroll on initial load if hash is present
    if (window.location.hash) {
      // Defer so the DOM is ready
      setTimeout(onHashChange, 100);
    }
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const handlePauseTask = async (taskId: string) => {
    // ponytail: no confirm — pause is reversible
    // #13: Optimistic update — immediately reflect in UI
    dashboard.optimisticStatusUpdate(taskId, "paused");
    try {
      if (grpcState === "connected") {
        const r = await grpcPauseTask(taskId);
        r.success ? showToast("Task paused", "success") : showToast(`Pause failed: ${r.error ?? "unknown"}`, "error");
      } else {
        const r = await api.pauseTask(taskId);
        r.success ? showToast("Task paused", "success") : showToast(`Pause failed: ${r.error ?? "unknown"}`, "error");
      }
    } catch (e) {
      // Revert optimistic update on failure
      dashboard.optimisticStatusUpdate(taskId, "in_progress");
      showToast(`Pause failed: ${String(e)}`, "error");
    }
  };
  const handleResumeTask = async (taskId: string) => {
    // ponytail: no confirm — resume is reversible
    // #13: Optimistic update — immediately reflect in UI
    dashboard.optimisticStatusUpdate(taskId, "in_progress");
    try {
      if (grpcState === "connected") {
        const r = await grpcResumeTask(taskId);
        r.success ? showToast("Task resumed", "success") : showToast(`Resume failed: ${r.error ?? "unknown"}`, "error");
      } else {
        const r = await api.resumeTask(taskId);
        r.success ? showToast("Task resumed", "success") : showToast(`Resume failed: ${r.error ?? "unknown"}`, "error");
      }
    } catch (e) {
      // Revert optimistic update on failure
      dashboard.optimisticStatusUpdate(taskId, "paused");
      showToast(`Resume failed: ${String(e)}`, "error");
    }
  };
  const handleResetCB = async () => {
    const ok = await confirmAction("Reset Circuit Breaker", "Force circuit breaker to closed state?");
    if (!ok) return;
    try { const r = await api.resetCircuitBreaker(); r.success ? showToast("Circuit breaker reset", "success") : showToast(`Reset failed: ${r.error ?? "unknown"}`, "error"); } catch (e) { showToast(`Reset failed: ${String(e)}`, "error"); }
  };
  const handleTriggerJob = async (jobId: string) => {
    const ok = await confirmAction("Trigger Job", `Trigger scheduled job?`);
    if (!ok) return;
    try { const r = await api.triggerJob(jobId); r.success ? showToast("Job triggered", "success") : showToast(`Trigger failed: ${r.error ?? "unknown"}`, "error"); } catch (e) { showToast(`Trigger failed: ${String(e)}`, "error"); }
  };
  const handleFlush = async () => {
    const ok = await confirmAction("Flush Pending Tasks", "Execute all queued tasks?");
    if (!ok) return;
    try { const r = await api.flushPending(); r.success ? showToast("Pending tasks flushed", "success") : showToast(`Flush failed: ${r.error ?? "unknown"}`, "error"); } catch (e) { showToast(`Flush failed: ${String(e)}`, "error"); }
  };

  // ── Auth gate ─────────────────────────────────────────────────
  if (auth.isChecking) {
    return (
      <div className="flex items-center justify-center min-h-screen text-[var(--text-secondary)]">
        <div className="text-center">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-400 mx-auto mb-3" />
          <p className="text-sm">Checking access...</p>
        </div>
      </div>
    );
  }

  if (!auth.isAuthenticated) {
    if (auth.connectionError) {
      return (
        <div className="flex items-center justify-center min-h-screen text-[var(--text-secondary)]">
          <div className="text-center max-w-md">
            <p className="text-lg font-semibold text-red-400 mb-2">Connection Error</p>
            <p className="text-sm mb-4">Unable to reach the server. Please check your network connection and try again.</p>
            <button
              onClick={() => window.location.reload()}
              className="px-4 py-2 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-500 transition-colors"
            >
              Retry
            </button>
          </div>
        </div>
      );
    }
    return (
      <div className="min-h-screen bg-[var(--bg-primary)]">
        <LoginModal onLogin={auth.login} />
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen text-[var(--text-secondary)]">
        <div className="text-center">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-400 mx-auto mb-3" />
          <p className="text-sm">Loading dashboard...</p>
        </div>
      </div>
    );
  }

  // All endpoints failed — server is likely down
  // ponytail: don't block if gRPC-Web is connected (REST may be down but gRPC works)
  const allFailed = Object.keys(dashboard.fetchErrors).length >= 5 && grpcState !== "connected";
  if (allFailed) {
    return (
      <div className="flex items-center justify-center min-h-screen text-[var(--text-secondary)]">
        <div className="text-center max-w-md">
          <p className="text-lg font-semibold text-red-400 mb-2">Unable to connect to server</p>
          <p className="text-sm mb-4">All dashboard endpoints failed. The backend may be down or unreachable.</p>
          <button
            onClick={() => {
              setLoading(true);
              dashboard.fetchInitial().then((errors) => {
                setLoading(false);
                if (Object.keys(errors).length > 0) showToast(`Some panels failed to load`, "error");
              });
            }}
            className="px-4 py-2 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-500 transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  // Data-source-aware stale: SSE-only panels (health, workers, scheduler, CB) are stale
  // when SSE disconnects; gRPC-backed panels (tasks, events) only stale when both disconnect.
  const sseStale = !connected;
  const grpcStale = !connected && grpcState !== "connected";

  return (
    <div className="text-[var(--text-primary)] min-h-screen">
      <ToastContainer />
      <ConfirmDialog />
      <Header connected={connected} grpcState={grpcState} lastUpdate={lastUpdate} theme={theme} onToggleTheme={toggleTheme} onLogout={auth.logout} fetchErrors={dashboard.fetchErrors} />
      <TaskSubmitForm grpcSubmitTask={grpcState === "connected" ? grpcSubmitTask : undefined} onTaskCreated={setHighlightTaskId} onOptimisticAdd={dashboard.optimisticAddTask} />
      <main className="max-w-7xl mx-auto px-4 py-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        <ErrorBoundary name="Health">
          <div id="health" className="md:col-span-2 scroll-mt-20"><HealthPanel data={healthWithGrpc} stale={sseStale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Circuit Breaker">
          <div id="circuit-breaker" className="scroll-mt-20"><CircuitBreakerPanel data={dashboard.circuitBreaker} onReset={handleResetCB} stale={sseStale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Workers">
          <div id="workers" className="scroll-mt-20"><WorkersPanel data={dashboard.workers} stale={sseStale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Tasks">
          <div id="tasks" className="scroll-mt-20"><TasksPanel data={dashboard.tasks} interactionLog={dashboard.interactionLog} onFlush={handleFlush} onPauseTask={handlePauseTask} onResumeTask={handleResumeTask} stale={grpcStale} highlightTaskId={highlightTaskId} onHighlightShown={() => setHighlightTaskId(null)} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Event Log">
          <div id="events" className="scroll-mt-20"><EventLogPanel events={dashboard.eventLog} stale={grpcStale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Scheduler">
          <div id="scheduler" className="md:col-span-2 scroll-mt-20"><SchedulerPanel data={dashboard.scheduler} onTriggerJob={handleTriggerJob} stale={sseStale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Task Activity">
          <div id="chart" className="md:col-span-2 scroll-mt-20"><TaskTrendChart tasks={dashboard.tasks} eventLog={dashboard.eventLog} stale={grpcStale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Code Search">
          <div id="search" className="md:col-span-2 scroll-mt-20"><SearchPanel grpcState={grpcState} /></div>
        </ErrorBoundary>
      </main>
      <ConnectionIndicator connected={connected} grpcState={grpcState} grpcExhausted={grpcExhausted} onReconnectSSE={sseReconnect} onReconnectGrpc={grpcConnect} onDisconnectGrpc={grpcDisconnect} />
    </div>
  );
}
export default App;
