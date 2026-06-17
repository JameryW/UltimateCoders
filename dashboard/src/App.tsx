import { useEffect, useRef } from "react";
import { useDashboard } from "@/hooks/useDashboard";
import { useSSE } from "@/hooks/useSSE";
import { useGrpcWeb } from "@/hooks/useGrpcWeb";
import type { GrpcConnectionState } from "@/hooks/useGrpcWeb";
import { useAuth } from "@/hooks/useAuth";
import { Header } from "@/components/layout/Header";
import { ConnectionIndicator } from "@/components/layout/ConnectionIndicator";
import { HealthPanel } from "@/components/panels/HealthPanel";
import { WorkersPanel } from "@/components/panels/WorkersPanel";
import { TasksPanel } from "@/components/panels/TasksPanel";
import { SchedulerPanel } from "@/components/panels/SchedulerPanel";
import { CircuitBreakerPanel } from "@/components/panels/CircuitBreakerPanel";
import { EventLogPanel } from "@/components/panels/EventLogPanel";
import { TaskTrendChart } from "@/components/charts/TaskTrendChart";
import { TaskSubmitForm } from "@/components/forms/TaskSubmitForm";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ToastContainer, showToast } from "@/components/ui/toast";
import { confirmAction } from "@/components/ui/confirm-dialog";
import * as api from "@/api/endpoints";
import type { TaskEvent } from "@/types/dashboard";

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

  const { connected } = useSSE({
    onUpdate: dashboard.handleSnapshot,
    onTaskEvent: dedupedHandleTaskEvent,
  });

  const { connectionState: grpcState, submitTask: grpcSubmitTask, healthCheck } = useGrpcWeb({
    onTaskEvent: dedupedHandleTaskEvent,
    enabled: true,
  });

  useEffect(() => { void dashboard.fetchInitial(); }, [dashboard.fetchInitial]);
  useEffect(() => { dashboard.setConnected(connected); }, [connected, dashboard.setConnected]);

  // Poll gRPC Health when connected — merge Rust engine components (local_worker, etc.)
  useEffect(() => {
    if (grpcState !== "connected") return;
    const poll = async () => {
      try {
        const h = await healthCheck();
        dashboard.mergeGrpcComponents(h.components);
      } catch { /* ignore */ }
    };
    void poll();
    const id = setInterval(() => { void poll(); }, 5000);
    return () => clearInterval(id);
  }, [grpcState, healthCheck, dashboard.mergeGrpcComponents]);

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

  return (
    <div className="text-gray-200 min-h-screen">
      <ToastContainer />
      <ConfirmDialog />
      <Header connected={connected} grpcState={grpcState} />
      <TaskSubmitForm grpcSubmitTask={grpcState === "connected" ? grpcSubmitTask : undefined} />
      <main className="max-w-7xl mx-auto px-4 py-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        <div className="md:col-span-2"><HealthPanel data={dashboard.health} /></div>
        <CircuitBreakerPanel data={dashboard.circuitBreaker} onReset={handleResetCB} />
        <WorkersPanel data={dashboard.workers} />
        <TasksPanel data={dashboard.tasks} interactionLog={dashboard.interactionLog} onFlush={handleFlush} onPauseTask={handlePauseTask} onResumeTask={handleResumeTask} />
        <EventLogPanel events={dashboard.eventLog} />
        <div className="md:col-span-2"><SchedulerPanel data={dashboard.scheduler} onTriggerJob={handleTriggerJob} /></div>
        <div className="md:col-span-2"><TaskTrendChart /></div>
      </main>
      <ConnectionIndicator connected={connected} grpcState={grpcState} />
    </div>
  );
}
export default App;
