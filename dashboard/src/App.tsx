import { useCallback, useEffect, useRef, useMemo, useState } from "react";
import { useDashboard } from "@/hooks/useDashboard";
import { useDashboardGrpc } from "@/hooks/useDashboardGrpc";
import { useGrpcWeb } from "@/hooks/useGrpcWeb";
import { useAuth } from "@/hooks/useAuth";
import { useTheme } from "@/hooks/useTheme";
import { TerminalDashboard } from "@/components/terminal/TerminalDashboard";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ToastContainer, showToast } from "@/components/ui/toast";
import { confirmAction } from "@/components/ui/confirm-dialog";
import { KeyboardShortcuts } from "@/components/ui/keyboard-shortcuts";
import type { GrpcTaskActionResult } from "@/hooks/useGrpcWeb";
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
  // ── Task selection ────────────────────────────────────────
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);

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

  /** Refresh every dashboard surface through the live Gateway before reporting status. */
  const fetchDashboardInitial = dashboard.fetchInitial;
  const refreshDashboard = useCallback(async () => {
    const errors = await fetchDashboardInitial({
      fetchWorkers: listWorkers,
      fetchScheduler: getSchedulerStatus,
      fetchEvents: listEvents,
      fetchTasks: listTasks,
    });
    if (Object.keys(errors).length > 0) {
      throw new Error(`refresh failed: ${Object.keys(errors).join(", ")}`);
    }
    setLastUpdate(new Date().toISOString());
  }, [fetchDashboardInitial, getSchedulerStatus, listEvents, listTasks, listWorkers]);

  const [loading, setLoading] = useState(true);

  // The operations surface can still render its disconnected state while the
  // Gateway is starting or temporarily unavailable.  `useAuth` reports that
  // condition separately from an actual authentication failure, so do not
  // leave the whole Dashboard behind a full-screen connection error.
  const authReady = auth.isAuthenticated || auth.connectionError;
  const hasFetchedRef = useRef(false);
  useEffect(() => {
    if (auth.isChecking || !authReady || hasFetchedRef.current) return;
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
  }, [auth.isChecking, authReady, grpcState, listWorkers, getSchedulerStatus, listEvents, listTasks]);

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

  const handlePauseTask = async (taskId: string): Promise<GrpcTaskActionResult> => {
    if (grpcState === "disconnected" || grpcExhausted) {
      const result = { success: false, taskId, status: "disconnected", error: "gRPC-Web disconnected" };
      showToast(`Pause failed: ${result.error}`, "error");
      return result;
    }
    dashboard.optimisticStatusUpdate(taskId, "paused");
    try {
      const r = await grpcPauseTask(taskId);
      if (r.success) showToast("Task paused", "success");
      // Revert the optimistic pause on server rejection (wrong state, etc.).
      else { dashboard.optimisticStatusUpdate(taskId, "in_progress"); showToast(`Pause failed: ${r.error ?? "unknown"}`, "error"); }
      return r;
    } catch (e) {
      dashboard.optimisticStatusUpdate(taskId, "in_progress");
      const result = { success: false, taskId, status: "error", error: String(e) };
      showToast(`Pause failed: ${result.error}`, "error");
      return result;
    }
  };
  const handleResumeTask = async (taskId: string): Promise<GrpcTaskActionResult> => {
    if (grpcState === "disconnected" || grpcExhausted) {
      const result = { success: false, taskId, status: "disconnected", error: "gRPC-Web disconnected" };
      showToast(`Resume failed: ${result.error}`, "error");
      return result;
    }
    dashboard.optimisticStatusUpdate(taskId, "in_progress");
    try {
      const r = await grpcResumeTask(taskId);
      if (r.success) showToast("Task resumed", "success");
      // Revert optimistic resume on server rejection — back to paused.
      else { dashboard.optimisticStatusUpdate(taskId, "paused"); showToast(`Resume failed: ${r.error ?? "unknown"}`, "error"); }
      return r;
    } catch (e) {
      dashboard.optimisticStatusUpdate(taskId, "paused");
      const result = { success: false, taskId, status: "error", error: String(e) };
      showToast(`Resume failed: ${result.error}`, "error");
      return result;
    }
  };
  const handleCancelTask = async (taskId: string): Promise<GrpcTaskActionResult> => {
    if (grpcState === "disconnected" || grpcExhausted) {
      const result = { success: false, taskId, status: "disconnected", error: "gRPC-Web disconnected" };
      showToast(`Cancel failed: ${result.error}`, "error");
      return result;
    }
    dashboard.optimisticStatusUpdate(taskId, "cancelled");
    try {
      const r = await grpcCancelTask(taskId);
      if (r.success) {
        // The task model represents cancellation as a terminal Failed state.
        // Reconcile the optimistic UI value with the authoritative response
        // because task_cancelled is an event, not a distinct task status.
        dashboard.optimisticStatusUpdate(taskId, r.status.toLowerCase());
        showToast("Task cancelled", "success");
      }
      // Revert optimistic cancel on server rejection — back to in_progress.
      else { dashboard.optimisticStatusUpdate(taskId, "in_progress"); showToast(`Cancel failed: ${r.error ?? "unknown"}`, "error"); }
      return r;
    } catch (e) {
      dashboard.optimisticStatusUpdate(taskId, "in_progress");
      const result = { success: false, taskId, status: "error", error: String(e) };
      showToast(`Cancel failed: ${result.error}`, "error");
      return result;
    }
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

  if (!auth.isAuthenticated && !auth.connectionError) {
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

  const grpcStale = grpcState !== "connected" && dashGrpcState !== "connected";

  return (
    <>
      <ToastContainer />
      <ConfirmDialog />
      <KeyboardShortcuts />
      <TerminalDashboard
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
        grpcStale={grpcStale}
        health={healthWithGrpc}
        workers={dashboard.workers}
        tasks={dashboard.tasks}
        scheduler={dashboard.scheduler}
        eventLog={dashboard.eventLog}
        metrics={dashboard.metrics}
        interactionLog={dashboard.interactionLog}
        selectedTask={selectedTask}
        selectedTaskId={selectedTaskId}
        onSelectTask={setSelectedTaskId}
        onPauseTask={handlePauseTask}
        onResumeTask={handleResumeTask}
        onCancelTask={handleCancelTask}
        onRefresh={refreshDashboard}
        onFlush={handleFlush}
        // Unary TaskService calls remain usable while the long-lived WatchTask
        // stream is reconnecting. The hook owns transport failures and returns
        // the actual server result to the command bar.
        grpcSubmitTask={grpcSubmitTask}
        onTaskCreated={(taskId) => setSelectedTaskId(taskId)}
        onOptimisticAdd={dashboard.optimisticAddTask}
      />
    </>
  );
}
export default App;
