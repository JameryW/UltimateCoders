import { useEffect, useRef, useMemo, useState, useCallback } from "react";
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
import { EventLogPanel } from "@/components/panels/EventLogPanel";
import { SearchPanel } from "@/components/panels/SearchPanel";
import { FileBrowser } from "@/components/panels/FileBrowser";
import type { FileBrowserNavigateEvent } from "@/components/panels/FileBrowser";
import { TaskDetail } from "@/components/panels/TaskDetail";
import { StatsBar } from "@/components/panels/StatsBar";
import { MetricsPanel } from "@/components/panels/MetricsPanel";
import { RepoManagementPanel } from "@/components/panels/RepoManagementPanel";
import { TaskTrendChart } from "@/components/charts/TaskTrendChart";
import { SidebarPanel } from "@/components/ui/sidebar-panel";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ToastContainer, showToast } from "@/components/ui/toast";
import { AlertBar } from "@/components/ui/alert-bar";
import { confirmAction } from "@/components/ui/confirm-dialog";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import { KeyboardShortcuts } from "@/components/ui/keyboard-shortcuts";
import type { TaskEvent, HealthData, TaskSummary } from "@/types/dashboard";

function eventKey(ev: TaskEvent): string {
  const dataHash = ev.type === "sync_required" ? "" : JSON.stringify(ev.data).slice(0, 80);
  return `${ev.task_id}:${ev.subtask_id ?? ""}:${ev.type}:${dataHash}`;
}

/** Login modal shown when auth is required but user is not authenticated. */
function LoginModal({ onLogin, loginError }: { onLogin: (password: string) => Promise<boolean>; loginError?: string | null }) {
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

  // Show server-returned login error if present
  const displayError = error || loginError;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      role="dialog"
      aria-modal="true"
      aria-label="Dashboard Login"
      onKeyDown={(e) => {
        if (e.key === "Escape") return;
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
        {displayError && <p className="text-sm text-red-400 mb-2">{displayError}</p>}
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
    // ponytail: proactive toasts for key async events
    switch (ev.type) {
      case "task_completed":
        showToast(`Task ${ev.task_id.slice(0, 8)} completed`, "success");
        break;
      case "task_failed":
        showToast(`Task ${ev.task_id.slice(0, 8)} failed`, "error");
        break;
      case "subtask_failed": {
        const err = ev.data.error ? `: ${String(ev.data.error).slice(0, 60)}` : "";
        showToast(`Subtask ${ev.subtask_id?.slice(0, 8) ?? "?"} failed${err}`, "error");
        break;
      }
    }
  };

  // Track last update timestamp for Header display
  const [lastUpdate, setLastUpdate] = useState<string | undefined>();
  // Track newly submitted task for highlight + auto-scroll
  const [highlightTaskId, setHighlightTaskId] = useState<string | null>(null);
  // Track file browser navigation from SearchPanel/OutputFiles
  const [fileBrowserNav, setFileBrowserNav] = useState<FileBrowserNavigateEvent | null>(null);

  // ── Sidebar state ─────────────────────────────────────────
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [collapsedPanels, setCollapsedPanels] = useState<Record<string, boolean>>({});
  const togglePanel = useCallback((key: string) => {
    setCollapsedPanels((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  // ── gRPC-Web hooks ─────────────────────────────────────────

  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { connectionState: grpcState, grpcExhausted, submitTask: grpcSubmitTask, healthCheck, connect: grpcConnect, disconnect: _grpcDisconnect, listTasks, pauseTask: grpcPauseTask, resumeTask: grpcResumeTask, cancelTask: grpcCancelTask } = useGrpcWeb({
    onTaskEvent: dedupedHandleTaskEvent,
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    onSyncRequired: (_reason: string, _skipped: number) => {
      needsSyncCountRef.current += 1;
      dashboard.setNeedsSync(true);
    },
    enabled: true,
  });

  const {
    connectionState: dashGrpcState,
    connect: dashGrpcConnect,
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    disconnect: _dashGrpcDisconnect,
    listWorkers,
    getSchedulerStatus,
    triggerSchedulerJob: grpcTriggerSchedulerJob,
    flushPendingTasks: grpcFlushPendingTasks,
    listEvents,
  } = useDashboardGrpc({
    onSnapshot: (snapshot) => {
      dashboard.handleSnapshot(snapshot);
      if (snapshot.health || snapshot.workers || snapshot.scheduler) {
        setLastUpdate(new Date().toISOString());
      }
    },
    onTaskEvent: (ev) => {
      dedupedHandleTaskEvent(ev);
      setLastUpdate(ev.timestamp);
    },
    mergeGrpcTasks: dashboard.mergeGrpcTasks,
    enabled: true,
  });

  const [loading, setLoading] = useState(true);

  const hasFetchedRef = useRef(false);
  useEffect(() => {
    if (auth.isChecking || !auth.isAuthenticated || hasFetchedRef.current) return;
    hasFetchedRef.current = true;
    dashboard.fetchInitial({
      fetchWorkers: listWorkers,
      fetchScheduler: getSchedulerStatus,
      fetchEvents: listEvents,
      // ponytail: F67 — always fetch the initial task list. skipTasks was tied
      // to grpcState, which the stream hooks flip to "connected" optimistically
      // at mount — so skipTasks was always true while fetchTasks was passed
      // under exactly that condition: the branches were mutually exclusive,
      // listTasks never ran, and the Tasks panel stayed "Unavailable" on
      // healthy boots.
      fetchTasks: listTasks,
    }).then((errors) => {
      setLoading(false);
      if (Object.keys(errors).length > 0) showToast(`Some panels failed to load`, "error");
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dashboard object changes every render (hook returns new object literal); its methods (fetchInitial) are stable useCallbacks
  }, [auth.isChecking, auth.isAuthenticated, grpcState, listWorkers, getSchedulerStatus, listEvents, listTasks]);

  useEffect(() => {
    if (dashGrpcState !== "connected" && grpcState === "connected") {
      listTasks().then((data) => {
        if (data.available) dashboard.mergeGrpcTasks(data);
      }).catch((err) => { console.warn("[Dashboard] listTasks failed (grpc→dash merge):", err); });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dashboard object changes every render; mergeGrpcTasks (stable useCallback) is already in deps
  }, [dashGrpcState, grpcState, listTasks, dashboard.mergeGrpcTasks]);

  useEffect(() => {
    if (needsSyncCountRef.current <= 0) return;
    needsSyncCountRef.current -= 1;
    if (grpcState === "connected") {
      listTasks().then((data) => {
        if (data.available) dashboard.mergeGrpcTasks(data);
      }).catch((err) => { console.warn("[Dashboard] listTasks failed (sync):", err); });
    }
    // ponytail: F75 - reset needsSync after handling so the NEXT sync_required
    // (true again) is a real state change and re-triggers this effect. Without
    // this, needsSync latched true forever after the first sync: a later
    // setNeedsSync(true) was a same-value no-op, the effect never re-ran, and
    // pending counts piled up in needsSyncCountRef - only drained when some
    // other dep (grpcState/listTasks/mergeGrpcTasks) happened to change. The
    // reset schedules one extra effect run, but by then the ref is already 0
    // so it early-returns (harmless).
    dashboard.setNeedsSync(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dashboard object changes every render; needsSync and mergeGrpcTasks (accessed via property) are already in deps
  }, [dashboard.needsSync, grpcState, listTasks, dashboard.mergeGrpcTasks]);

  const [grpcHealthComponents, setGrpcHealthComponents] = useState<{ name: string; status: string; details?: string }[]>([]);
  useEffect(() => {
    if (grpcState !== "connected") {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- conditional reset on disconnect; no cascading render (state is already empty)
      setGrpcHealthComponents([]);
      return;
    }
    const poll = async () => {
      try {
        const h = await healthCheck();
        setGrpcHealthComponents(h.components);
        setLastUpdate(new Date().toISOString());
      } catch (err) { console.warn("[Dashboard] Health poll failed:", err); }
    };
    poll();
    const timer = setInterval(poll, 30000);
    return () => clearInterval(timer);
  }, [grpcState, healthCheck]);

  const healthWithGrpc = useMemo<HealthData>(() => {
    const grpcStatus = grpcState === "connected" ? "ok"
      : grpcState === "connecting" ? "degraded"
      : grpcState === "error" || grpcState === "reconnecting" ? "error"
      : "unavailable";
    const grpcComponent = { name: "gRPC-Web", status: grpcStatus };
    const components = [...dashboard.health.components];
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
        if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    };
    window.addEventListener("hashchange", onHashChange);
    if (window.location.hash) setTimeout(onHashChange, 100);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const handlePauseTask = async (taskId: string) => {
    dashboard.optimisticStatusUpdate(taskId, "paused");
    try {
      if (grpcState === "connected") {
        const r = await grpcPauseTask(taskId);
        if (r.success) showToast("Task paused", "success");
        // ponytail: revert the optimistic pause on server rejection (wrong
        // state, etc.) — otherwise the UI shows paused while the server didn't
        // pause it, until a sync re-pulls the truth. catch (throw) path already
        // reverts; this covers the r.success===false path.
        else { dashboard.optimisticStatusUpdate(taskId, "in_progress"); showToast(`Pause failed: ${r.error ?? "unknown"}`, "error"); }
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
        if (r.success) showToast("Task resumed", "success");
        // ponytail: revert optimistic resume on server rejection — back to paused.
        else { dashboard.optimisticStatusUpdate(taskId, "paused"); showToast(`Resume failed: ${r.error ?? "unknown"}`, "error"); }
      }
    } catch (e) {
      dashboard.optimisticStatusUpdate(taskId, "paused");
      showToast(`Resume failed: ${String(e)}`, "error");
    }
  };
  const handleCancelTask = async (taskId: string) => {
    dashboard.optimisticStatusUpdate(taskId, "cancelled");
    try {
      if (grpcState === "connected") {
        const r = await grpcCancelTask(taskId);
        if (r.success) showToast("Task cancelled", "success");
        // ponytail: revert optimistic cancel on server rejection — back to in_progress.
        else { dashboard.optimisticStatusUpdate(taskId, "in_progress"); showToast(`Cancel failed: ${r.error ?? "unknown"}`, "error"); }
      }
    } catch (e) {
      dashboard.optimisticStatusUpdate(taskId, "in_progress");
      showToast(`Cancel failed: ${String(e)}`, "error");
    }
  };
  const handleTriggerJob = async (jobId: string) => {
    const ok = await confirmAction("Trigger Job", `Trigger scheduled job?`);
    if (!ok) return;
    try { const r = await grpcTriggerSchedulerJob(jobId); if (r.success) showToast("Job triggered", "success"); else showToast(`Trigger failed: ${r.error ?? "unknown"}`, "error"); } catch (e) { showToast(`Trigger failed: ${String(e)}`, "error"); }
  };
  const handleFlush = async () => {
    const ok = await confirmAction("Flush Pending Tasks", "Execute all queued tasks?");
    if (!ok) return;
    try { const r = await grpcFlushPendingTasks(); if (r.success) showToast("Pending tasks flushed", "success"); else showToast(`Flush failed: ${r.error ?? "unknown"}`, "error"); } catch (e) { showToast(`Flush failed: ${String(e)}`, "error"); }
  };

  // Selected task for detail view
  const selectedTask: TaskSummary | null = useMemo(
    () => dashboard.tasks.tasks.find((t) => t.id === selectedTaskId) ?? null,
    [dashboard.tasks.tasks, selectedTaskId],
  );

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
            <button onClick={() => window.location.reload()} className="px-4 py-2 text-sm rounded-md bg-blue-600 text-white hover:bg-blue-500 transition-colors">
              Retry
            </button>
          </div>
        </div>
      );
    }
    return (
      <div className="min-h-screen bg-[var(--bg-primary)]">
        <LoginModal onLogin={auth.login} loginError={auth.loginError} />
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
                fetchEvents: listEvents,
                fetchTasks: (grpcState as string) === "connected" ? listTasks : undefined,
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

  const grpcStale = grpcState !== "connected" && dashGrpcState !== "connected";

  // Sidebar panel summaries for collapsed state
  const workersSummary = dashboard.workers.available ? `${dashboard.workers.available_count}/${dashboard.workers.total} online` : undefined;
  const healthSummary = healthWithGrpc.available ? healthWithGrpc.status : undefined;
  const schedulerSummary = dashboard.scheduler.available ? (dashboard.scheduler.is_running ? "Running" : "Stopped") : undefined;

  return (
    <div className="text-[var(--text-primary)] min-h-screen">
      <ToastContainer />
      <ConfirmDialog />
      <KeyboardShortcuts />
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
      <main className="max-w-[1440px] mx-auto px-6 py-6">
        <StatsBar tasks={dashboard.tasks} workers={dashboard.workers} eventLog={dashboard.eventLog} metrics={dashboard.metrics} stale={grpcStale} />
        <AlertBar
          workers={dashboard.workers}
          eventLog={dashboard.eventLog}
          metrics={dashboard.metrics}
          activeAlerts={dashboard.activeAlerts}
          onJumpWorkers={() => document.getElementById("workers")?.scrollIntoView({ behavior: "smooth" })}
        />
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 mt-4">
          {/* ── Left column (8/12): Core panels ────────────── */}
          <div className="lg:col-span-8 space-y-6">
            <ErrorBoundary name="Tasks">
              <div id="tasks" className="scroll-mt-20">
                <TasksPanel
                  data={dashboard.tasks}
                  interactionLog={dashboard.interactionLog}
                  onFlush={handleFlush}
                  onPauseTask={handlePauseTask}
                  onResumeTask={handleResumeTask}
                  onCancelTask={handleCancelTask}
                  stale={grpcStale}
                  highlightTaskId={highlightTaskId}
                  onHighlightShown={() => setHighlightTaskId(null)}
                  onNavigateFile={(nav) => setFileBrowserNav(nav)}
                  grpcSubmitTask={grpcState === "connected" ? grpcSubmitTask : undefined}
                  onTaskCreated={setHighlightTaskId}
                  onOptimisticAdd={dashboard.optimisticAddTask}
                  onSelectTask={setSelectedTaskId}
                  selectedTaskId={selectedTaskId}
                />
              </div>
            </ErrorBoundary>

            <ErrorBoundary name="Event Log">
              <div id="events" className="scroll-mt-20">
                <EventLogPanel events={dashboard.eventLog} stale={grpcStale} onSelectTask={setSelectedTaskId} />
              </div>
            </ErrorBoundary>

            <ErrorBoundary name="Task Activity">
              <div id="chart" className="scroll-mt-20">
                <TaskTrendChart tasks={dashboard.tasks} eventLog={dashboard.eventLog} stale={grpcStale} />
              </div>
            </ErrorBoundary>

            <ErrorBoundary name="Metrics">
              <MetricsPanel metrics={dashboard.metrics} stale={grpcStale} />
            </ErrorBoundary>

            <ErrorBoundary name="Code Search">
              <div id="search" className="scroll-mt-20">
                <SearchPanel grpcState={grpcState} onNavigateFile={(nav) => setFileBrowserNav(nav)} stale={grpcStale} />
              </div>
            </ErrorBoundary>

            <ErrorBoundary name="Files">
              <div id="files" className="scroll-mt-20">
                <FileBrowser initialNav={fileBrowserNav} onNavConsumed={() => setFileBrowserNav(null)} stale={grpcStale} />
              </div>
            </ErrorBoundary>

            <ErrorBoundary name="Repos">
              <div id="repos" className="scroll-mt-20">
                <RepoManagementPanel />
              </div>
            </ErrorBoundary>
          </div>

          {/* ── Right column (4/12): Sidebar ────────────── */}
          <div className="lg:col-span-4 space-y-4">
            {selectedTask ? (
              /* Task Detail view */
              <div className="rounded-xl border border-[var(--border-color)] bg-[var(--bg-surface)] shadow-sm">
                <div className="flex items-center gap-3 px-5 py-3 border-b border-[var(--border-color)]">
                  <button
                    onClick={() => setSelectedTaskId(null)}
                    className="p-1 rounded-md hover:bg-[var(--bg-surface-alt)] transition-colors"
                    aria-label="Back to panels"
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 text-[var(--text-secondary)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
                    </svg>
                  </button>
                  <h2 className="text-base font-semibold text-[var(--text-primary)] truncate">
                    Task Detail
                  </h2>
                </div>
                <div className="p-5">
                  <ErrorBoundary name="Task Detail">
                    <TaskDetail
                      task={selectedTask}
                      interactionLog={dashboard.interactionLog[selectedTask.id] ?? []}
                      onNavigateFile={(nav) => setFileBrowserNav(nav)}
                      repoId={selectedTask.project_id || undefined}
                    />
                  </ErrorBoundary>
                </div>
              </div>
            ) : (
              /* Collapsible sidebar panels */
              <>
                <ErrorBoundary name="Workers">
                  <SidebarPanel
                    title="Workers"
                    id="workers"
                    className="scroll-mt-20"
                    summary={workersSummary}
                    summaryVariant="ok"
                    collapsed={collapsedPanels.workers}
                    onToggle={() => togglePanel("workers")}
                    stale={grpcStale}
                  >
                    <WorkersPanel workers={dashboard.workers} tasks={dashboard.tasks} stale={grpcStale} onJumpTask={setSelectedTaskId} embedded />
                  </SidebarPanel>
                </ErrorBoundary>

                <ErrorBoundary name="Health">
                  <SidebarPanel
                    title="Engine Health"
                    id="health"
                    className="scroll-mt-20"
                    summary={healthSummary}
                    summaryVariant={healthSummary === "ok" ? "ok" : healthSummary === "degraded" ? "degraded" : healthSummary === "error" ? "error" : "unavailable"}
                    collapsed={collapsedPanels.health}
                    onToggle={() => togglePanel("health")}
                    stale={grpcStale}
                  >
                    <HealthPanel data={healthWithGrpc} stale={grpcStale} embedded />
                  </SidebarPanel>
                </ErrorBoundary>

                <ErrorBoundary name="Scheduler">
                  <SidebarPanel
                    title="Scheduler"
                    id="scheduler"
                    className="scroll-mt-20"
                    summary={schedulerSummary}
                    summaryVariant={dashboard.scheduler.available ? (dashboard.scheduler.is_running ? "ok" : "degraded") : "unavailable"}
                    collapsed={collapsedPanels.scheduler}
                    onToggle={() => togglePanel("scheduler")}
                    stale={grpcStale}
                  >
                    <SchedulerPanel data={dashboard.scheduler} onTriggerJob={handleTriggerJob} stale={grpcStale} embedded />
                  </SidebarPanel>
                </ErrorBoundary>
              </>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
export default App;
