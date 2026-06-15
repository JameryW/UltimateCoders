import { useEffect } from "react";
import { useDashboard } from "@/hooks/useDashboard";
import { useSSE } from "@/hooks/useSSE";
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

function App() {
  // Auth placeholder (no-op for now)
  void useAuth();

  const dashboard = useDashboard();

  // SSE connection
  const { connected } = useSSE({
    onUpdate: dashboard.handleSnapshot,
    onTaskEvent: dashboard.handleTaskEvent,
  });

  // Fetch initial data on mount
  useEffect(() => {
    void dashboard.fetchInitial();
  }, [dashboard.fetchInitial]);

  // Sync connected state back to dashboard
  useEffect(() => {
    dashboard.setConnected(connected);
  }, [connected, dashboard.setConnected]);

  // Action handlers
  const handlePauseTask = async (taskId: string) => {
    const ok = await confirmAction(
      "Pause Task",
      `Pause task ${taskId.substring(0, 8)}?`,
    );
    if (!ok) return;
    try {
      const result = await api.pauseTask(taskId);
      if (result.success) showToast("Task paused", "success");
      else showToast(`Pause failed: ${result.error ?? "unknown"}`, "error");
    } catch (e) {
      showToast(`Pause failed: ${String(e)}`, "error");
    }
  };

  const handleResumeTask = async (taskId: string) => {
    const ok = await confirmAction(
      "Resume Task",
      `Resume task ${taskId.substring(0, 8)}?`,
    );
    if (!ok) return;
    try {
      const result = await api.resumeTask(taskId);
      if (result.success) showToast("Task resumed", "success");
      else showToast(`Resume failed: ${result.error ?? "unknown"}`, "error");
    } catch (e) {
      showToast(`Resume failed: ${String(e)}`, "error");
    }
  };

  const handleResetCB = async () => {
    const ok = await confirmAction(
      "Reset Circuit Breaker",
      "Force circuit breaker to closed state?",
    );
    if (!ok) return;
    try {
      const result = await api.resetCircuitBreaker();
      if (result.success) showToast("Circuit breaker reset", "success");
      else showToast(`Reset failed: ${result.error ?? "unknown"}`, "error");
    } catch (e) {
      showToast(`Reset failed: ${String(e)}`, "error");
    }
  };

  const handleTriggerJob = async (jobId: string) => {
    const ok = await confirmAction(
      "Trigger Job",
      `Trigger scheduled job?`,
    );
    if (!ok) return;
    try {
      const result = await api.triggerJob(jobId);
      if (result.success) showToast("Job triggered", "success");
      else showToast(`Trigger failed: ${result.error ?? "unknown"}`, "error");
    } catch (e) {
      showToast(`Trigger failed: ${String(e)}`, "error");
    }
  };

  const handleFlush = async () => {
    const ok = await confirmAction(
      "Flush Pending Tasks",
      "Execute all queued tasks?",
    );
    if (!ok) return;
    try {
      const result = await api.flushPending();
      if (result.success) showToast("Pending tasks flushed", "success");
      else showToast(`Flush failed: ${result.error ?? "unknown"}`, "error");
    } catch (e) {
      showToast(`Flush failed: ${String(e)}`, "error");
    }
  };

  return (
    <div className="text-gray-200 min-h-screen">
      <ToastContainer />
      <ConfirmDialog />

      <Header connected={connected} />

      <TaskSubmitForm />

      <main className="max-w-7xl mx-auto px-4 py-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        {/* Row 1: Health (2-col), Circuit Breaker, Workers */}
        <div className="md:col-span-2">
          <HealthPanel data={dashboard.health} />
        </div>
        <CircuitBreakerPanel
          data={dashboard.circuitBreaker}
          onReset={handleResetCB}
        />
        <WorkersPanel data={dashboard.workers} />

        {/* Row 2: Tasks, Event Log */}
        <TasksPanel
          data={dashboard.tasks}
          interactionLog={dashboard.interactionLog}
          onFlush={handleFlush}
          onPauseTask={handlePauseTask}
          onResumeTask={handleResumeTask}
        />
        <EventLogPanel events={dashboard.eventLog} />

        {/* Row 3: Scheduler (2-col), Chart (2-col) */}
        <div className="md:col-span-2">
          <SchedulerPanel
            data={dashboard.scheduler}
            onTriggerJob={handleTriggerJob}
          />
        </div>
        <div className="md:col-span-2">
          <TaskTrendChart />
        </div>
      </main>

      <ConnectionIndicator connected={connected} />
    </div>
  );
}

export default App;
