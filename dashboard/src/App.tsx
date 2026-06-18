import { useEffect, useRef, useMemo, useState } from "react";
import { useDashboard } from "@/hooks/useDashboard";
import { useSSE } from "@/hooks/useSSE";
import { useGrpcWeb } from "@/hooks/useGrpcWeb";
import { useAuth } from "@/hooks/useAuth";
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
  // Include a data hash so pause→resume→start within 2s doesn't get deduped.
  const dataHash = ev.type === "sync_required" ? "" : JSON.stringify(ev.data).slice(0, 40);
  return `${ev.task_id}:${ev.subtask_id ?? ""}:${ev.type}:${dataHash}`;
}

function App() {
  void useAuth();
  const dashboard = useDashboard();

  // Dedup: track last-seen key → timestamp to allow re-processing after a
  // reasonable window (same type for same entity can happen again later).
  const seenKeys = useRef(new Map<string, number>());
  const dedupedHandleTaskEvent = (ev: TaskEvent) => {
    const key = eventKey(ev);
    const now = Date.now();
    const lastSeen = seenKeys.current.get(key);
    // Skip if we saw this exact event key within the last 2 seconds
    if (lastSeen !== undefined && now - lastSeen < 2000) return;
    seenKeys.current.set(key, now);
    // Prune entries older than 60s to bound memory
    if (seenKeys.current.size > 5000) {
      for (const [k, ts] of seenKeys.current) {
        if (now - ts > 60_000) seenKeys.current.delete(k);
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
  });

  const { connectionState: grpcState, submitTask: grpcSubmitTask, healthCheck, connect: grpcConnect, listTasks, pauseTask: grpcPauseTask, resumeTask: grpcResumeTask } = useGrpcWeb({
    onTaskEvent: dedupedHandleTaskEvent,
    enabled: true,
  });

  // Loading state -- show spinner until initial data arrives
  const [loading, setLoading] = useState(true);
  // ponytail: fetchInitial always loads all data; gRPC connect does its own listTasks merge.
  // Old skipTasks logic was broken — grpcState is always "disconnected" on first render.
  useEffect(() => {
    dashboard.fetchInitial().finally(() => setLoading(false));
  }, [dashboard.fetchInitial]);
  useEffect(() => { dashboard.setConnected(connected); }, [connected, dashboard.setConnected]);

  // gRPC-Web fallback: when SSE unavailable but gRPC connected, fetch tasks via gRPC
  useEffect(() => {
    if (!connected && grpcState === "connected") {
      listTasks().then((data) => {
        if (data.available) dashboard.mergeGrpcTasks(data);
      }).catch(() => { /* ignore */ });
    }
  }, [connected, grpcState, listTasks, dashboard.mergeGrpcTasks]);

  // Handle sync_required from broadcast lag — do full listTasks re-sync
  useEffect(() => {
    if (dashboard.needsSync && grpcState === "connected") {
      dashboard.setNeedsSync(false);
      listTasks().then((data) => {
        if (data.available) dashboard.mergeGrpcTasks(data);
      }).catch(() => { /* ignore */ });
    } else if (dashboard.needsSync) {
      dashboard.setNeedsSync(false);
    }
  }, [dashboard.needsSync, grpcState, listTasks, dashboard.mergeGrpcTasks, dashboard.setNeedsSync]);

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
      : grpcState === "error" || grpcState === "exhausted" ? "error"
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

  const handlePauseTask = async (taskId: string) => {
    const ok = await confirmAction("Pause Task", `Pause task ${taskId.substring(0, 8)}?`);
    if (!ok) return;
    try {
      // ponytail: prefer gRPC-Web, fall back to REST
      if (grpcState === "connected") {
        const r = await grpcPauseTask(taskId);
        r.success ? showToast("Task paused", "success") : showToast(`Pause failed: ${r.error ?? "unknown"}`, "error");
      } else {
        const r = await api.pauseTask(taskId);
        r.success ? showToast("Task paused", "success") : showToast(`Pause failed: ${r.error ?? "unknown"}`, "error");
      }
    } catch (e) { showToast(`Pause failed: ${String(e)}`, "error"); }
  };
  const handleResumeTask = async (taskId: string) => {
    const ok = await confirmAction("Resume Task", `Resume task ${taskId.substring(0, 8)}?`);
    if (!ok) return;
    try {
      if (grpcState === "connected") {
        const r = await grpcResumeTask(taskId);
        r.success ? showToast("Task resumed", "success") : showToast(`Resume failed: ${r.error ?? "unknown"}`, "error");
      } else {
        const r = await api.resumeTask(taskId);
        r.success ? showToast("Task resumed", "success") : showToast(`Resume failed: ${r.error ?? "unknown"}`, "error");
      }
    } catch (e) { showToast(`Resume failed: ${String(e)}`, "error"); }
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

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen text-gray-400">
        <div className="text-center">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-400 mx-auto mb-3" />
          <p className="text-sm">Loading dashboard…</p>
        </div>
      </div>
    );
  }

  // ponytail: panels are stale only when both SSE and gRPC-Web are disconnected;
  // if either source is live, data is still flowing.
  const stale = !connected && grpcState !== "connected";

  return (
    <div className="text-gray-200 min-h-screen">
      <ToastContainer />
      <ConfirmDialog />
      <Header connected={connected} grpcState={grpcState} lastUpdate={lastUpdate} />
      <TaskSubmitForm grpcSubmitTask={grpcState === "connected" ? grpcSubmitTask : undefined} onTaskCreated={setHighlightTaskId} />
      <main className="max-w-7xl mx-auto px-4 py-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        <ErrorBoundary name="Health">
          <div className="md:col-span-2"><HealthPanel data={healthWithGrpc} stale={stale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Circuit Breaker">
          <CircuitBreakerPanel data={dashboard.circuitBreaker} onReset={handleResetCB} stale={stale} />
        </ErrorBoundary>
        <ErrorBoundary name="Workers">
          <WorkersPanel data={dashboard.workers} stale={stale} />
        </ErrorBoundary>
        <ErrorBoundary name="Tasks">
          <TasksPanel data={dashboard.tasks} interactionLog={dashboard.interactionLog} onFlush={handleFlush} onPauseTask={handlePauseTask} onResumeTask={handleResumeTask} stale={stale} highlightTaskId={highlightTaskId} onHighlightShown={() => setHighlightTaskId(null)} />
        </ErrorBoundary>
        <ErrorBoundary name="Event Log">
          <EventLogPanel events={dashboard.eventLog} stale={stale} />
        </ErrorBoundary>
        <ErrorBoundary name="Scheduler">
          <div className="md:col-span-2"><SchedulerPanel data={dashboard.scheduler} onTriggerJob={handleTriggerJob} stale={stale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Task Activity">
          <div className="md:col-span-2"><TaskTrendChart tasks={dashboard.tasks} eventLog={dashboard.eventLog} stale={stale} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Code Search">
          <div className="md:col-span-2"><SearchPanel grpcState={grpcState} /></div>
        </ErrorBoundary>
      </main>
      <ConnectionIndicator connected={connected} grpcState={grpcState} onReconnectSSE={sseReconnect} onReconnectGrpc={grpcConnect} />
    </div>
  );
}
export default App;
