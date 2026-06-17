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
import type { TaskEvent, HealthData } from "@/types/dashboard";

function eventKey(ev: TaskEvent): string {
  return `${ev.task_id}:${ev.type}:${ev.timestamp}`;
}

function App() {
  void useAuth();
  const dashboard = useDashboard();

  const seenKeys = useRef(new Set<string>());
  const dedupedHandleTaskEvent = (ev: TaskEvent) => {
    const key = eventKey(ev);
    if (seenKeys.current.has(key)) return;
    seenKeys.current.add(key);
    if (seenKeys.current.size > 10_000) seenKeys.current.clear();
    dashboard.handleTaskEvent(ev);
  };

  const { connected, reconnect: sseReconnect } = useSSE({
    onUpdate: dashboard.handleSnapshot,
    onTaskEvent: dedupedHandleTaskEvent,
  });

  const { connectionState: grpcState, submitTask: grpcSubmitTask, healthCheck, connect: grpcConnect } = useGrpcWeb({
    onTaskEvent: dedupedHandleTaskEvent,
    enabled: true,
  });

  // Loading state — show spinner until initial data arrives
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    dashboard.fetchInitial().finally(() => setLoading(false));
  }, [dashboard.fetchInitial]);
  useEffect(() => { dashboard.setConnected(connected); }, [connected, dashboard.setConnected]);

  // Periodic gRPC health check — poll every 30s when connected
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
      : grpcState === "error" ? "error"
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
    try { const r = await api.pauseTask(taskId); r.success ? showToast("Task paused", "success") : showToast(`Pause failed: ${r.error ?? "unknown"}`, "error"); } catch (e) { showToast(`Pause failed: ${String(e)}`, "error"); }
  };
  const handleResumeTask = async (taskId: string) => {
    const ok = await confirmAction("Resume Task", `Resume task ${taskId.substring(0, 8)}?`);
    if (!ok) return;
    try { const r = await api.resumeTask(taskId); r.success ? showToast("Task resumed", "success") : showToast(`Resume failed: ${r.error ?? "unknown"}`, "error"); } catch (e) { showToast(`Resume failed: ${String(e)}`, "error"); }
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

  return (
    <div className="text-gray-200 min-h-screen">
      <ToastContainer />
      <ConfirmDialog />
      <Header connected={connected} grpcState={grpcState} />
      <TaskSubmitForm grpcSubmitTask={grpcState === "connected" ? grpcSubmitTask : undefined} />
      <main className="max-w-7xl mx-auto px-4 py-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        <ErrorBoundary name="Health">
          <div className="md:col-span-2"><HealthPanel data={healthWithGrpc} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Circuit Breaker">
          <CircuitBreakerPanel data={dashboard.circuitBreaker} onReset={handleResetCB} />
        </ErrorBoundary>
        <ErrorBoundary name="Workers">
          <WorkersPanel data={dashboard.workers} />
        </ErrorBoundary>
        <ErrorBoundary name="Tasks">
          <TasksPanel data={dashboard.tasks} interactionLog={dashboard.interactionLog} onFlush={handleFlush} onPauseTask={handlePauseTask} onResumeTask={handleResumeTask} />
        </ErrorBoundary>
        <ErrorBoundary name="Event Log">
          <EventLogPanel events={dashboard.eventLog} />
        </ErrorBoundary>
        <ErrorBoundary name="Scheduler">
          <div className="md:col-span-2"><SchedulerPanel data={dashboard.scheduler} onTriggerJob={handleTriggerJob} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Task Activity">
          <div className="md:col-span-2"><TaskTrendChart tasks={dashboard.tasks} eventLog={dashboard.eventLog} /></div>
        </ErrorBoundary>
        <ErrorBoundary name="Code Search">
          <div className="md:col-span-2"><SearchPanel /></div>
        </ErrorBoundary>
      </main>
      <ConnectionIndicator connected={connected} grpcState={grpcState} onReconnectSSE={sseReconnect} onReconnectGrpc={grpcConnect} />
    </div>
  );
}
export default App;
