import { useEffect, useRef, useMemo, useState } from "react";
import { useDashboard } from "@/hooks/useDashboard";
import { useDashboardGrpc } from "@/hooks/useDashboardGrpc";
import { useGrpcWeb } from "@/hooks/useGrpcWeb";
import { useAuth } from "@/hooks/useAuth";
import { useTheme } from "@/hooks/useTheme";
import { Header } from "@/components/layout/Header";
import { HealthPanel } from "@/components/panels/HealthPanel";
import { WorkersPanel } from "@/components/panels/WorkersPanel";
import { TasksPanel } from "@/components/panels/TasksPanel";
import { SchedulerPanel } from "@/components/panels/SchedulerPanel";
import { CircuitBreakerPanel } from "@/components/panels/CircuitBreakerPanel";
import { EventLogPanel } from "@/components/panels/EventLogPanel";
import { SearchPanel } from "@/components/panels/SearchPanel";
import { FileBrowser } from "@/components/panels/FileBrowser";
import type { FileBrowserNavigateEvent } from "@/components/panels/FileBrowser";
import { TaskTrendChart } from "@/components/charts/TaskTrendChart";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ToastContainer, showToast } from "@/components/ui/toast";
import { confirmAction } from "@/components/ui/confirm-dialog";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import type { TaskEvent, HealthData } from "@/types/dashboard";

function eventKey(ev: TaskEvent): string {
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

  // Dedup: content key + 2s window for gRPC-Web events
  const seenContentKeys = useRef(new Map<string, number>());
  const needsSyncCountRef = useRef(0);
  const dedupedHandleTaskEvent = (ev: TaskEvent) => {
    // gRPC-Web: content key + 1s window dedup
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
    dashboard.handleTaskEvent(ev);
  };

  // Track last update timestamp for Header display
  const [lastUpdate, setLastUpdate] = useState<string | undefined>();
  // Track newly submitted task for highlight + auto-scroll
  const [highlightTaskId, setHighlightTaskId] = useState<string | null>(null);
  // Track file browser navigation from SearchPanel/OutputFiles
  const [fileBrowserNav, setFileBrowserNav] = useState<FileBrowserNavigateEvent | null>(null);

  // ── gRPC-Web hooks ─────────────────────────────────────────

  // TaskService: WatchTask stream + task operations
  const { connectionState: grpcState, grpcExhausted, submitTask: grpcSubmitTask, healthCheck, connect: grpcConnect, disconnect: grpcDisconnect, listTasks, pauseTask: grpcPauseTask, resumeTask: grpcResumeTask } = useGrpcWeb({
    onTaskEvent: dedupedHandleTaskEvent,
    onSyncRequired: (_reason: string, _skipped: number) => {
      needsSyncCountRef.current += 1;
      dashboard.setNeedsSync(true);
    },
    enabled: true,
  });

  // DashboardService: WatchDashboard stream + dashboard operations
  const {
    connectionState: dashGrpcState,
    connect: dashGrpcConnect,
    disconnect: dashGrpcDisconnect,
    listWorkers,
    getSchedulerStatus,
    getCircuitBreakerStatus,
    resetCircuitBreaker: grpcResetCircuitBreaker,
    triggerSchedulerJob: grpcTriggerSchedulerJob,
    flushPendingTasks: grpcFlushPendingTasks,
    listEvents,
  } = useDashboardGrpc({
    onSnapshot: (snapshot) => {
      dashboard.handleSnapshot(snapshot);
      if (snapshot.health || snapshot.workers || snapshot.scheduler || snapshot.circuitBreaker) {
        setLastUpdate(new Date().toISOString());
      }
    },
    onTaskEvent: (ev) => {
      dedupedHandleTaskEvent(ev);
      setLastUpdate(ev.timestamp);
    },
    enabled: true,
  });

  // Loading state -- show spinner until initial data arrives
  const [loading, setLoading] = useState(true);

  // Fetch initial data once auth is confirmed via gRPC-Web
  const hasFetchedRef = useRef(false);
  useEffect(() => {
    if (auth.isChecking || !auth.isAuthenticated || hasFetchedRef.current) return;
    hasFetchedRef.current = true;
    const skipTasks = grpcState === "connected";
    dashboard.fetchInitial({
      skipTasks,
      fetchWorkers: listWorkers,
      fetchScheduler: getSchedulerStatus,
      fetchCircuitBreaker: getCircuitBreakerStatus,
      fetchEvents: listEvents,
      fetchTasks: grpcState === "connected" ? listTasks : undefined,
    }).then((errors) => {
      setLoading(false);
      if (Object.keys(errors).length > 0) showToast(`Some panels failed to load`, "error");
    });
  }, [auth.isChecking, auth.isAuthenticated, grpcState]);

  // gRPC-Web fallback: when WatchDashboard unavailable but TaskService connected, fetch tasks via gRPC
  useEffect(() => {
    if (dashGrpcState !== "connected" && grpcState === "connected") {
      listTasks().then((data) => {
        if (data.available) dashboard.mergeGrpcTasks(data);
      }).catch(() => { /* ignore */ });
    }
  }, [dashGrpcState, grpcState, listTasks, dashboard.mergeGrpcTasks]);

  // Handle sync_required from broadcast lag -- re-sync via gRPC
  useEffect(() => {
    if (needsSyncCountRef.current <= 0) return;
    needsSyncCountRef.current -= 1;
    if (grpcState === "connected") {
      listTasks().then((data) => {
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
      } catch { /* ignore -- next poll will retry */ }
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
    dashboard.optimisticStatusUpdate(taskId, "paused");
    try {
      if (grpcState === "connected") {
        const r = await grpcPauseTask(taskId);
        r.success ? showToast("Task paused", "success") : showToast(`Pause failed: ${r.error ?? "unknown"}`, "error");
      }
    } catch (e) {
      dashboard.optimisticStatusUpdate(taskId, "in_progress");
      showToast(`Pause failed: ${String(e)}`, "error");
    }
  };
  const handleResumeTask = async (taskId: string) => {
    dashboard.optimisticStatusUpdate(taskId, "in_progress");
    try {
      if (grpcState === "connected") {
        const r = await grpcResumeTask(taskId);
        r.success ? showToast("Task resumed", "success") : showToast(`Resume failed: ${r.error ?? "unknown"}`, "error");
      }
    } catch (e) {
      dashboard.optimisticStatusUpdate(taskId, "paused");
      showToast(`Resume failed: ${String(e)}`, "error");
    }
  };
  const handleResetCB = async () => {
    const ok = await confirmAction("Reset Circuit Breaker", "Force circuit breaker to closed state?");
    if (!ok) return;
    try { const r = await grpcResetCircuitBreaker(); r.success ? showToast("Circuit breaker reset", "success") : showToast(`Reset failed: ${r.error ?? "unknown"}`, "error"); } catch (e) { showToast(`Reset failed: ${String(e)}`, "error"); }
  };
  const handleTriggerJob = async (jobId: string) => {
    const ok = await confirmAction("Trigger Job", `Trigger scheduled job?`);
    if (!ok) return;
    try { const r = await grpcTriggerSchedulerJob(jobId); r.success ? showToast("Job triggered", "success") : showToast(`Trigger failed: ${r.error ?? "unknown"}`, "error"); } catch (e) { showToast(`Trigger failed: ${String(e)}`, "error"); }
  };
  const handleFlush = async () => {
    const ok = await confirmAction("Flush Pending Tasks", "Execute all queued tasks?");
    if (!ok) return;
    try { const r = await grpcFlushPendingTasks(); r.success ? showToast("Pending tasks flushed", "success") : showToast(`Flush failed: ${r.error ?? "unknown"}`, "error"); } catch (e) { showToast(`Flush failed: ${String(e)}`, "error"); }
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

  // All endpoints failed -- server is likely down
  const allFailed = Object.keys(dashboard.fetchErrors).length >= 3 && grpcState !== "connected" && dashGrpcState !== "connected";
  if (allFailed) {
    return (
      <div className="flex items-center justify-center min-h-screen text-[var(--text-secondary)]">
        <div className="text-center max-w-md">
          <p className="text-lg font-semibold text-red-400 mb-2">Unable to connect to server</p>
          <p className="text-sm mb-4">All gRPC endpoints failed. The backend may be down or unreachable.</p>
          <button
            onClick={() => {
              setLoading(true);
              dashboard.fetchInitial({
                fetchWorkers: listWorkers,
                fetchScheduler: getSchedulerStatus,
                fetchCircuitBreaker: getCircuitBreakerStatus,
                fetchEvents: listEvents,
                fetchTasks: grpcState === "connected" ? listTasks : undefined,
              }).then((errors) => {
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

  // Stale state: when gRPC connections are down
  const grpcStale = grpcState !== "connected" && dashGrpcState !== "connected";

  return (
    <div className="text-[var(--text-primary)] min-h-screen">
      <ToastContainer />
      <ConfirmDialog />
      <Header
        connected={dashGrpcState === "connected" || grpcState === "connected"}
        grpcState={grpcState}
        grpcExhausted={grpcExhausted}
        dashGrpcState={dashGrpcState}
        lastUpdate={lastUpdate}
        theme={theme}
        onToggleTheme={toggleTheme}
        onLogout={auth.logout}
        onReconnectGrpc={grpcConnect}
        onReconnectDashGrpc={dashGrpcConnect}
        fetchErrors={dashboard.fetchErrors}
      />
      <main className="max-w-7xl mx-auto px-4 py-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        {/* Row 1: Tasks (2-col, submit form inline) + Workers(1) + CB(1) */}
        <ErrorBoundary name="Tasks">
          <div id="tasks" className="md:col-span-2 scroll-mt-20">
            <TasksPanel
              data={dashboard.tasks}
              interactionLog={dashboard.interactionLog}
              onFlush={handleFlush}
              onPauseTask={handlePauseTask}
              onResumeTask={handleResumeTask}
              stale={grpcStale}
              highlightTaskId={highlightTaskId}
              onHighlightShown={() => setHighlightTaskId(null)}
              onNavigateFile={(nav) => setFileBrowserNav(nav)}
              grpcSubmitTask={grpcState === "connected" ? grpcSubmitTask : undefined}
              onTaskCreated={setHighlightTaskId}
              onOptimisticAdd={dashboard.optimisticAddTask}
            />
          </div>
        </ErrorBoundary>
        <ErrorBoundary name="Workers">
          <div id="workers" className="scroll-mt-20"><WorkersPanel workers={dashboard.workers} tasks={dashboard.tasks} stale={grpcStale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Circuit Breaker">
          <div id="circuit-breaker" className="scroll-mt-20"><CircuitBreakerPanel data={dashboard.circuitBreaker} onReset={handleResetCB} stale={grpcStale} /></div>
        </ErrorBoundary>

        {/* Row 2: EventLog(2) + Search(2) */}
        <ErrorBoundary name="Event Log">
          <div id="events" className="md:col-span-2 scroll-mt-20"><EventLogPanel events={dashboard.eventLog} stale={grpcStale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Code Search">
          <div id="search" className="md:col-span-2 scroll-mt-20"><SearchPanel grpcState={grpcState} onNavigateFile={(nav) => setFileBrowserNav(nav)} stale={grpcStale} /></div>
        </ErrorBoundary>

        {/* Row 3: FileBrowser(2) + Health(1) + Scheduler(1) */}
        <ErrorBoundary name="Files">
          <div id="files" className="md:col-span-2 scroll-mt-20"><FileBrowser initialNav={fileBrowserNav} onNavConsumed={() => setFileBrowserNav(null)} stale={grpcStale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Health">
          <div id="health" className="scroll-mt-20"><HealthPanel data={healthWithGrpc} stale={grpcStale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Scheduler">
          <div id="scheduler" className="scroll-mt-20"><SchedulerPanel data={dashboard.scheduler} onTriggerJob={handleTriggerJob} stale={grpcStale} /></div>
        </ErrorBoundary>

        {/* Row 4: Chart(2) */}
        <ErrorBoundary name="Task Activity">
          <div id="chart" className="md:col-span-2 scroll-mt-20"><TaskTrendChart tasks={dashboard.tasks} eventLog={dashboard.eventLog} stale={grpcStale} /></div>
        </ErrorBoundary>
      </main>
    </div>
  );
}
export default App;
