import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EventLogPanel } from "@/components/panels/EventLogPanel";
import { FileBrowser, type FileBrowserNavigateEvent } from "@/components/panels/FileBrowser";
import { SearchPanel } from "@/components/panels/SearchPanel";
import { TaskDetail } from "@/components/panels/TaskDetail";
import { TasksPanel } from "@/components/panels/TasksPanel";
import { WorkersPanel } from "@/components/panels/WorkersPanel";
import { InferencePanel } from "@/components/panels/InferencePanel";
import type { GrpcConnectionState, GrpcSubmitResult, GrpcTaskActionResult } from "@/hooks/useGrpcWeb";
import type { DashboardConnectionState } from "@/hooks/useDashboardGrpc";
import type { Theme } from "@/hooks/useTheme";
import { executeUcCommand } from "@/lib/ucCommands";
import { latestDashboardEvents } from "@/lib/dashboardEvents";
import { statusBadgeClass } from "@/lib/utils";
import type {
  DashboardEvent,
  HealthData,
  MetricsSnapshot,
  SchedulerData,
  TaskEvent,
  TasksData,
  TaskSummary,
  WorkersData,
} from "@/types/dashboard";

type TerminalView = "overview" | "tasks" | "workers" | "search" | "memory" | "logs" | "inference";

const VIEWS: Array<{ id: TerminalView; label: string; meta: string; key: string; icon: string }> = [
  { id: "overview", label: "概览", meta: "CLUSTER", key: "1", icon: "◯" },
  { id: "tasks", label: "任务", meta: "DAG", key: "2", icon: "▤" },
  { id: "workers", label: "工作节点", meta: "WORKERS", key: "3", icon: "⬡" },
  { id: "search", label: "检索", meta: "SEARCH", key: "4", icon: "⌕" },
  { id: "memory", label: "记忆", meta: "MEMORY", key: "5", icon: "◌" },
  { id: "logs", label: "日志", meta: "EVENTS", key: "6", icon: "☷" },
  { id: "inference", label: "推理实验", meta: "INFERENCE", key: "7", icon: "◈" },
];

interface TerminalDashboardProps {
  connected: boolean;
  grpcState: GrpcConnectionState;
  grpcExhausted: boolean;
  dashGrpcState: DashboardConnectionState;
  lastUpdate?: string;
  theme: Theme;
  onToggleTheme: () => void;
  onLogout: () => void;
  onReconnectGrpc: () => void;
  onReconnectDashGrpc: () => void;
  fetchErrors: Record<string, string>;
  grpcStale: boolean;
  health: HealthData;
  workers: WorkersData;
  tasks: TasksData;
  scheduler: SchedulerData;
  eventLog: DashboardEvent[];
  metrics: MetricsSnapshot | null;
  interactionLog: Record<string, TaskEvent[]>;
  selectedTask: TaskSummary | null;
  selectedTaskId: string | null;
  taskHistoryLoading: boolean;
  taskHistoryError?: string;
  onSelectTask: (taskId: string | null) => void;
  onPauseTask: (taskId: string) => Promise<GrpcTaskActionResult>;
  onResumeTask: (taskId: string) => Promise<GrpcTaskActionResult>;
  onCancelTask: (taskId: string) => Promise<GrpcTaskActionResult>;
  onRefresh: () => Promise<void>;
  onFlush: () => void;
  grpcSubmitTask?: (description: string, projectId: string) => Promise<GrpcSubmitResult>;
  onTaskCreated: (taskId: string) => void;
  onOptimisticAdd: (taskId: string, description: string, projectId: string, subtaskCount: number, subtasks?: TaskSummary["subtasks"]) => void;
}

function shortId(value: string, length = 10): string {
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

function percent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(Number.isFinite(value) ? value : 0)));
}

function formatUptime(seconds?: number): string {
  if (!seconds || seconds < 0) return "00:00:00";
  const hours = Math.floor(seconds / 3600).toString().padStart(2, "0");
  const minutes = Math.floor((seconds % 3600) / 60).toString().padStart(2, "0");
  const remainder = Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${hours}:${minutes}:${remainder}`;
}

function formatClock(date: Date): string {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

type BarTone = "green" | "blue" | "amber" | "orange" | "purple";

const ACTIVE_TASK_STATUSES = new Set(["in_progress", "running", "planning", "assigned"]);
const RUNNING_SUBTASK_STATUSES = new Set(["in_progress", "running", "assigned", "started", "executing"]);
const DONE_SUBTASK_STATUSES = new Set(["completed", "done", "success", "succeeded"]);
const TERMINAL_TASK_STATUSES = new Set(["completed", "failed", "cancelled", "canceled", "done"]);

function MiniBar({ value, tone = "green" }: { value: number; tone?: BarTone }) {
  return (
    <span className={`terminal-mini-bar ${tone}`}>
      <i style={{ width: `${percent(value)}%` }} />
    </span>
  );
}

function EffortMeter({ value }: { value: number }) {
  const filled = Math.max(0, Math.min(4, Math.ceil(percent(value) / 25)));
  return (
    <span className="cc-effort" aria-label={`effort ${percent(value)}%`}>
      {Array.from({ length: 4 }, (_, index) => (
        <i key={index} className={index < filled ? "on" : ""} />
      ))}
    </span>
  );
}

function ConnectionPill({ label, state }: { label: string; state: string }) {
  const online = state === "connected";
  const pending = state === "connecting" || state === "reconnecting";
  return (
    <span className={`terminal-connection ${online ? "online" : pending ? "pending" : "offline"}`}>
      <i /> {label} · {online ? "connected" : pending ? "connecting" : "disconnected"}
    </span>
  );
}

function RoleLegend() {
  return (
    <div className="cc-legend" aria-label="Role legend">
      <span className="cc-legend-item lead"><i /> gateway · lead</span>
      <span className="cc-legend-item swarm"><i /> workers · swarm</span>
      <span className="cc-legend-item dispatch"><i /> dispatch · queue</span>
      <span className="cc-legend-item advisor"><i /> health · advisor</span>
    </div>
  );
}

function eventAdviceLine(event: DashboardEvent): string {
  const detailKeys = Object.keys(event.details ?? {});
  if (detailKeys.length === 0) return event.type.replace(/_/g, " ");
  const preview = detailKeys.slice(0, 2).map((key) => {
    const value = event.details[key];
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      return `${key}=${String(value).slice(0, 28)}`;
    }
    return key;
  }).join(" · ");
  return `${event.type.replace(/_/g, " ")} · ${preview}`;
}

function pickLeadTask(tasks: TasksData, selectedTask: TaskSummary | null): TaskSummary | null {
  const list = tasks.tasks ?? [];
  if (selectedTask && ACTIVE_TASK_STATUSES.has(selectedTask.status)) return selectedTask;
  const active = list.find((task) => ACTIVE_TASK_STATUSES.has(task.status));
  if (active) return active;
  if (selectedTask) return selectedTask;
  return list[0] ?? null;
}

function collectSwarmCards(lead: TaskSummary | null, workers: WorkersData) {
  const subtasks = (lead?.subtasks ?? [])
    .filter((subtask) => RUNNING_SUBTASK_STATUSES.has(subtask.status) || typeof subtask.percent === "number")
    .slice(0, 3)
    .map((subtask) => {
      const done = DONE_SUBTASK_STATUSES.has(subtask.status) || subtask.step_status === "completed" || percent(subtask.percent ?? 0) >= 100;
      const running = !done && (RUNNING_SUBTASK_STATUSES.has(subtask.status) || subtask.step_status === "started");
      return {
        id: subtask.id,
        title: shortId(subtask.id, 12),
        detail: subtask.phase || subtask.step_summary || subtask.description || "subtask",
        value: percent(subtask.percent ?? (done ? 100 : running ? 35 : 0)),
        done,
        running,
      };
    });

  if (subtasks.length > 0) return subtasks;

  return (workers.workers ?? []).slice(0, 3).map((worker) => {
    const load = percent(worker.load_percent);
    return {
      id: worker.id,
      title: shortId(worker.id, 12),
      detail: worker.is_available ? "available" : "offline",
      value: load,
      done: load <= 0 && worker.is_available,
      running: worker.is_available && load > 0,
    };
  });
}

function reviewCue(lead: TaskSummary | null, selectedTask: TaskSummary | null): { title: string; detail: string } {
  const focus = selectedTask ?? lead;
  if (!focus) return { title: "review · idle", detail: "awaiting task selection" };
  if (TERMINAL_TASK_STATUSES.has(focus.status)) {
    return { title: `review · ${focus.status}`, detail: "verify outputs · inspect history" };
  }
  if (ACTIVE_TASK_STATUSES.has(focus.status)) {
    return { title: "review · in flight", detail: "awaiting verify after swarm completes" };
  }
  return { title: `review · ${focus.status}`, detail: shortId(focus.id, 16) };
}

function AdvisorPanel({
  connected,
  workers,
  tasks,
  eventLog,
  health,
  metrics,
  onView,
  compact = false,
}: Pick<TerminalDashboardProps, "connected" | "workers" | "tasks" | "eventLog" | "health" | "metrics"> & {
  onView: (view: TerminalView) => void;
  compact?: boolean;
}) {
  const workerList = workers.workers ?? [];
  const onlineWorkers = workerList.filter((worker) => worker.is_available).length;
  const advice = latestDashboardEvents(eventLog ?? [], 4);
  const components = (health.components ?? []).slice(0, 4);
  const phase = !connected
    ? "waiting for gateway"
    : health.status === "ok" || health.status === "healthy"
      ? "cluster healthy"
      : health.status || "checking health";

  return (
    <aside className={`cc-advisor ${compact ? "compact" : ""}`} aria-label="Health advisor">
      <div className="cc-advisor-head">
        <span>HEALTH advisor · on call</span>
        <b className={connected ? "online" : "offline"}>{connected ? "live" : "offline"}</b>
      </div>
      <div className="cc-advisor-phase"><span className="cc-diamond">◆</span> {phase}</div>
      <div className="cc-advisor-section">
        <div className="cc-advisor-label">components</div>
        {components.length === 0 ? (
          <div className="cc-advisor-empty">no health components yet</div>
        ) : components.map((component) => (
          <div key={component.name} className="cc-advisor-line">
            <span>◇ {component.name}</span>
            <b>{component.status}</b>
          </div>
        ))}
      </div>
      <div className="cc-advisor-section">
        <div className="cc-advisor-label">last advice</div>
        {advice.length === 0 ? (
          <div className="cc-advisor-empty">» waiting for events</div>
        ) : advice.map((event, index) => (
          <button key={`${event.timestamp}-${event.type}-${index}`} className="cc-advisor-advice" onClick={() => onView("logs")}>
            » {eventAdviceLine(event)}
          </button>
        ))}
      </div>
      <div className="cc-advisor-metrics">
        <div><span>uptime</span><b>{formatUptime(health.uptime_seconds ?? metrics?.system.uptime_seconds)}</b></div>
        <div><span>events</span><b>{eventLog.length}</b></div>
        <div><span>workers</span><b>{onlineWorkers}/{workers.total}</b></div>
        <div><span>tasks</span><b>{tasks.total}</b></div>
        <div><span>util</span><b>{(metrics?.system.cluster_utilization_pct ?? 0).toFixed(0)}%</b></div>
        <div><span>engine</span><b>{health.version ?? "—"}</b></div>
      </div>
      <div className="cc-advisor-foot">
        <span>◇ {health.status || "unknown"}</span>
        <button onClick={() => onView("logs")}>session log</button>
      </div>
    </aside>
  );
}

function OverviewView({
  health,
  workers,
  tasks,
  eventLog,
  metrics,
  connected,
  selectedTask,
  onView,
  onSelectTask,
}: Pick<TerminalDashboardProps, "health" | "workers" | "tasks" | "eventLog" | "metrics" | "connected" | "selectedTask"> & {
  onView: (view: TerminalView) => void;
  onSelectTask: (taskId: string | null) => void;
}) {
  const workerList = workers.workers ?? [];
  const onlineWorkers = workerList.filter((worker) => worker.is_available).length;
  const averageLoad = workerList.length
    ? workerList.reduce((sum, worker) => sum + percent(worker.load_percent), 0) / workerList.length
    : 0;
  const runningTasks = (tasks.tasks ?? []).filter((task) => ACTIVE_TASK_STATUSES.has(task.status)).length;
  const pending = tasks.pending_task_count ?? 0;
  const onlineRatio = workers.total ? (onlineWorkers / workers.total) * 100 : 0;
  const queuePercent = tasks.total ? Math.min(100, (pending / Math.max(tasks.total, 1)) * 100) : pending > 0 ? 100 : 0;
  const lead = pickLeadTask(tasks, selectedTask);
  const swarm = collectSwarmCards(lead, workers);
  const review = reviewCue(lead, selectedTask);
  const sessionEvents = latestDashboardEvents(eventLog ?? [], 8);

  return (
    <div className="terminal-overview cc-board">
      <RoleLegend />
      <div className="cc-layout">
        <AdvisorPanel
          connected={connected}
          workers={workers}
          tasks={tasks}
          eventLog={eventLog}
          health={health}
          metrics={metrics}
          onView={onView}
        />

        <section className="cc-flow" aria-label="Live execution flow">
          <button
            className="cc-card lead"
            onClick={() => {
              if (lead) {
                onSelectTask(lead.id);
                onView("tasks");
              } else {
                onView("tasks");
              }
            }}
          >
            <div className="cc-card-topline">
              <strong>gateway · lead</strong>
              <EffortMeter value={averageLoad || (ACTIVE_TASK_STATUSES.has(lead?.status ?? "") ? 50 : 0)} />
            </div>
            <div className="cc-card-title">{lead ? shortId(lead.description || lead.id, 48) : "no active task"}</div>
            <div className="cc-card-meta">
              <span>{lead ? lead.status : connected ? "idle" : "offline"}</span>
              <span>{runningTasks} running · engine {health.version ?? "—"}</span>
            </div>
          </button>

          <div className="cc-flow-connector" aria-hidden="true"><i /><span /><b>▼</b></div>

          <button className="cc-card dispatch" onClick={() => onView("workers")}>
            <div className="cc-card-topline">
              <strong>dispatch · fork layer</strong>
              <span className="cc-card-count">queue {pending}</span>
            </div>
            <div className="cc-dispatch-bars">
              <div className="cc-dispatch-row"><span>worker load</span><MiniBar value={averageLoad} tone="orange" /><b>{percent(averageLoad)}%</b></div>
              <div className="cc-dispatch-row"><span>pending queue</span><MiniBar value={queuePercent} tone="green" /><b>{pending}</b></div>
              <div className="cc-dispatch-row"><span>online ratio</span><MiniBar value={onlineRatio} tone="blue" /><b>{onlineWorkers}/{workers.total}</b></div>
            </div>
          </button>

          <div className="cc-flow-connector" aria-hidden="true"><i /><span /><b>▼</b></div>

          <div className="cc-swarm-head">delegate to workers · swarm</div>
          <div className="cc-swarm-row">
            {swarm.length === 0 ? (
              <div className="cc-swarm-empty">no running subtasks or workers</div>
            ) : swarm.map((card) => (
              <button
                key={card.id}
                className="cc-card swarm"
                onClick={() => onView(lead?.subtasks?.length ? "tasks" : "workers")}
                title={card.id}
              >
                <strong>{card.title}</strong>
                <MiniBar value={card.value} tone="blue" />
                <div className="cc-swarm-status">
                  <span className={card.done ? "done" : card.running ? "running" : "idle"}>
                    {card.done ? "✓ done" : card.running ? "◐ running" : "◇ idle"}
                  </span>
                  <b>{card.value}%</b>
                </div>
                <small>{card.detail}</small>
              </button>
            ))}
          </div>

          <div className="cc-flow-connector" aria-hidden="true"><i /><span /><b>▼</b></div>

          <button
            className="cc-card review"
            onClick={() => {
              if (selectedTask ?? lead) {
                onSelectTask((selectedTask ?? lead)!.id);
                onView("tasks");
              } else {
                onView("tasks");
              }
            }}
          >
            <div className="cc-card-topline"><strong>{review.title}</strong></div>
            <div className="cc-card-meta"><span>{review.detail}</span></div>
          </button>
        </section>
      </div>

      <section className="cc-session-log" aria-label="Session log">
        <div className="cc-session-log-head">
          <span>session log</span>
          <button onClick={() => onView("logs")}>all events</button>
        </div>
        {sessionEvents.length === 0 ? (
          <div className="terminal-empty-line">waiting for live events…</div>
        ) : sessionEvents.map((event, index) => (
          <button
            key={`${event.timestamp}-${event.type}-${index}`}
            className="cc-session-row"
            onClick={() => {
              const taskId = typeof event.details.task_id === "string" ? event.details.task_id : null;
              if (taskId) {
                onSelectTask(taskId);
                onView("tasks");
              } else {
                onView("logs");
              }
            }}
          >
            <time>{new Date(event.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}</time>
            <span className="cc-session-role">{event.type.split("_")[0] || "uc"}</span>
            <span className="cc-session-msg">{eventAdviceLine(event)}</span>
          </button>
        ))}
      </section>

      <p className="terminal-help-line">提示：顶栏 <kbd>1</kbd>–<kbd>7</kbd> 切视图 · 左栏主操作 · <kbd>Ctrl</kbd><kbd>K</kbd> 聚焦命令 · <kbd>/</kbd> 命令行</p>
    </div>
  );
}

function AdvisorRail({
  connected,
  workers,
  tasks,
  eventLog,
  health,
  metrics,
  onView,
}: Pick<TerminalDashboardProps, "connected" | "workers" | "tasks" | "eventLog" | "health" | "metrics"> & { onView: (view: TerminalView) => void }) {
  return (
    <div className="terminal-cluster-rail cc-rail-shell">
      <AdvisorPanel
        connected={connected}
        workers={workers}
        tasks={tasks}
        eventLog={eventLog}
        health={health}
        metrics={metrics}
        onView={onView}
        compact
      />
    </div>
  );
}

function StatusPillBar({
  connected,
  grpcState,
  dashGrpcState,
  workers,
  tasks,
  eventLog,
}: Pick<TerminalDashboardProps, "connected" | "grpcState" | "dashGrpcState" | "workers" | "tasks" | "eventLog">) {
  const onlineWorkers = (workers.workers ?? []).filter((worker) => worker.is_available).length;
  const runningTasks = (tasks.tasks ?? []).filter((task) => ACTIVE_TASK_STATUSES.has(task.status)).length;
  return (
    <div className="cc-status-pills" aria-label="Live status">
      <span className={`cc-pill swarm ${onlineWorkers > 0 ? "hot" : ""}`}>workers [{onlineWorkers}/{workers.total}]</span>
      <span className={`cc-pill lead ${runningTasks > 0 ? "hot" : ""}`}>tasks [{runningTasks} running]</span>
      <span className={`cc-pill dispatch ${(tasks.pending_task_count ?? 0) > 0 ? "hot" : ""}`}>queue [{tasks.pending_task_count ?? 0}]</span>
      <span className={`cc-pill ${grpcState === "connected" ? "hot lead" : ""}`}>grpc [{grpcState}]</span>
      <span className={`cc-pill ${dashGrpcState === "connected" ? "hot advisor" : "advisor"}`}>dash [{dashGrpcState}]</span>
      <span className="cc-pill advisor">events [{eventLog.length}]</span>
      <span className={`cc-pill ${connected ? "hot dispatch" : ""}`}>{connected ? "backend [online]" : "backend [offline]"}</span>
    </div>
  );
}

export function TerminalDashboard({
  connected,
  grpcState,
  grpcExhausted,
  dashGrpcState,
  lastUpdate,
  theme,
  onToggleTheme,
  onLogout,
  onReconnectGrpc,
  onReconnectDashGrpc,
  fetchErrors,
  grpcStale,
  health,
  workers,
  tasks,
  eventLog,
  metrics,
  interactionLog,
  selectedTask,
  selectedTaskId,
  taskHistoryLoading,
  taskHistoryError,
  onSelectTask,
  onPauseTask,
  onResumeTask,
  onCancelTask,
  onRefresh,
  onFlush,
  grpcSubmitTask,
  onTaskCreated,
  onOptimisticAdd,
}: TerminalDashboardProps) {
  const [view, setView] = useState<TerminalView>("overview");
  const [command, setCommand] = useState("");
  const [lastCommand, setLastCommand] = useState("—");
  const [notice, setNotice] = useState({ text: "Dashboard 就绪 — 正在读取真实 Docker Gateway", tone: "info" });
  const [clock, setClock] = useState(() => new Date());
  const [busy, setBusy] = useState(false);
  const [fileBrowserNav, setFileBrowserNav] = useState<FileBrowserNavigateEvent | null>(null);
  const commandRef = useRef<HTMLInputElement>(null);
  const historyRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (view === "tasks" && selectedTaskId && window.innerWidth <= 1300) {
      historyRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }, [selectedTaskId, view]);

  const selectedTaskForCommand = selectedTaskId ?? selectedTask?.id ?? tasks.tasks[0]?.id;
  const runCommand = useCallback(async (rawValue = command) => {
    const raw = rawValue.trim();
    if (!raw) return;
    setLastCommand(raw);
    setBusy(true);
    try {
      const result = await executeUcCommand(raw, selectedTaskForCommand, {
        refresh: onRefresh,
        pause: onPauseTask,
        resume: onResumeTask,
        cancel: onCancelTask,
        submit: async (description) => {
          if (!grpcSubmitTask) {
            return {
              success: false,
              taskId: "",
              status: "TaskService unavailable",
              subtaskCount: 0,
              subtasks: [],
            };
          }
          const submitResult = await grpcSubmitTask(description, "");
          if (submitResult.success) {
            onOptimisticAdd(submitResult.taskId, description, "", submitResult.subtaskCount, submitResult.subtasks.map((subtask) => ({
              id: subtask.id,
              description: subtask.description,
              status: subtask.status,
              depends_on: subtask.dependsOn,
              assigned_worker: subtask.assignedWorker,
            })));
            onTaskCreated(submitResult.taskId);
          }
          return submitResult;
        },
      });
      if (result.view) setView(result.view);
      setNotice({ text: result.message, tone: result.tone });
    } finally {
      setBusy(false);
      setCommand("");
    }
  }, [command, grpcSubmitTask, onCancelTask, onOptimisticAdd, onPauseTask, onRefresh, onResumeTask, onTaskCreated, selectedTaskForCommand]);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        commandRef.current?.focus();
      }
      if (event.key === "/" && document.activeElement?.tagName !== "INPUT") {
        event.preventDefault();
        commandRef.current?.focus();
      }
      const numeric = Number(event.key);
      const next = VIEWS.find((item) => item.key === String(numeric));
      if (next && document.activeElement?.tagName !== "INPUT") setView(next.id);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const currentView = useMemo(() => VIEWS.find((item) => item.id === view) ?? VIEWS[0]!, [view]);
  const runQuickCommand = (value: string) => { void runCommand(value); };

  return (
    <div className={`terminal-app ${view === "overview" ? "cc-mode" : ""}`} data-theme={theme}>
      <header className="terminal-topbar">
        <div className="terminal-window-dots" aria-hidden="true"><i className="red" /><i className="yellow" /><i className="green" /></div>
        <div className={`terminal-brand ${connected ? "online" : "offline"}`}><span>ULTIMATE</span><strong>CODERS</strong></div>
        <div className="terminal-brand-tag">command center · live status</div>
        <nav className="terminal-tabs" aria-label="视图切换">
          <span className="terminal-tabs-label">视图</span>
          {VIEWS.map((item) => <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)}><b>{item.key}</b>{item.label}<small>{item.meta}</small></button>)}
        </nav>
        <div className="terminal-topbar-status"><i className={connected ? "online" : ""} />{connected ? "NATS" : "OFFLINE"}<small>{workers.total} workers</small><time>{formatClock(clock)}</time></div>
        <div className="terminal-topbar-connections"><ConnectionPill label="gRPC" state={grpcState} /><ConnectionPill label="Dash" state={dashGrpcState} /></div>
        <button className="terminal-theme-toggle" onClick={onToggleTheme} title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}>{theme === "dark" ? "☼" : "◐"}</button>
        <button className="terminal-logout" onClick={onLogout} title="Logout">↪</button>
      </header>

      <div className="terminal-mobile-actions" aria-label="快捷操作">
        <button onClick={() => runQuickCommand("status")}>❯ status</button>
        <button onClick={() => { setView("tasks"); setCommand("submit "); commandRef.current?.focus(); }}>❯ submit</button>
        <button onClick={() => runQuickCommand(`pause ${selectedTaskForCommand ?? ""}`)} disabled={busy}>❯ pause</button>
        <button onClick={() => runQuickCommand(`resume ${selectedTaskForCommand ?? ""}`)} disabled={busy}>❯ resume</button>
        <button onClick={() => setNotice({ text: "事件流由后端推送，不生成假事件", tone: "info" })}>❯ freeze</button>
        <button onClick={() => setView("search")}>⌕ search</button>
        <button onClick={() => runQuickCommand("help")}>? help</button>
      </div>

      <div className="terminal-workspace">
        <aside className="terminal-command-rail">
          <div className="terminal-command-rail-title"><span>快捷操作</span><b>5</b></div>
          <div className="terminal-command-rail-hint">主操作 ×5 · history / 低频进 <kbd>Ctrl+K</kbd><br /><span>↵ 执行</span></div>
          <div className="terminal-command-group-label">主操作</div>
          <button className="selected" onClick={() => runQuickCommand("status")}><span>❯</span><strong>status</strong><small>集群摘要</small></button>
          <button onClick={() => { setView("tasks"); setCommand("submit "); commandRef.current?.focus(); }}><span>❯</span><strong>submit</strong><small>入队…</small></button>
          <button onClick={() => runQuickCommand(`pause ${selectedTaskForCommand ?? ""}`)} disabled={busy}><span>❯</span><strong>pause</strong><small>{selectedTaskForCommand ? shortId(selectedTaskForCommand) : "选择任务"}</small></button>
          <button onClick={() => runQuickCommand(`resume ${selectedTaskForCommand ?? ""}`)} disabled={busy}><span>❯</span><strong>resume</strong><small>{selectedTaskForCommand ? shortId(selectedTaskForCommand) : "选择任务"}</small></button>
          <button onClick={() => setNotice({ text: "事件流由后端推送，不生成假事件", tone: "info" })}><span>❯</span><strong>freeze</strong><small>暂停提示</small></button>
          <div className="terminal-command-more"><span>更多命令</span><b>search · help · history</b><kbd>Ctrl+K</kbd></div>
          <div className="terminal-last-command"><span>最近运行</span><b>{lastCommand}</b><small>执行命令后记在这里</small></div>
          <div className="terminal-rail-spacer" />
          <div className="terminal-rail-stat"><b>{workers.total}</b><span>工作节点在线</span></div>
          <div className="terminal-rail-stat"><b>{tasks.total}</b><span>DAG 任务</span></div>
          <div className="terminal-rail-stat wide"><b>gRPC-Web</b><span>真实 Docker Gateway</span></div>
        </aside>

        <section className="terminal-main-panel">
          <div className="terminal-panel-head"><div><h1>{currentView.label}</h1><span>uc / {currentView.meta.toLowerCase()}</span></div><div className={`terminal-panel-meta ${connected ? "online" : "offline"}`}><i />{health.version ?? "engine"} · {connected ? "connected" : "waiting"}</div></div>
          <div className="terminal-view-body">
            {view === "overview" && <OverviewView health={health} workers={workers} tasks={tasks} eventLog={eventLog} metrics={metrics} connected={connected} selectedTask={selectedTask} onView={setView} onSelectTask={onSelectTask} />}
            {view === "tasks" && <div className="terminal-data-view terminal-task-layout">
              <TasksPanel data={tasks} interactionLog={interactionLog} onFlush={onFlush} onPauseTask={onPauseTask} onResumeTask={onResumeTask} onCancelTask={onCancelTask} stale={grpcStale} grpcSubmitTask={grpcSubmitTask} onTaskCreated={onTaskCreated} onOptimisticAdd={onOptimisticAdd} onSelectTask={onSelectTask} selectedTaskId={selectedTaskId} showInlineDetail={false} onNavigateFile={setFileBrowserNav} />
              <section className="terminal-task-history" ref={historyRef} aria-label="Selected task history">
                <div className="terminal-task-history-head">
                  <span>任务状态与历史日志</span>
                  {selectedTask && <span className={statusBadgeClass(selectedTask.status)}>{selectedTask.status}</span>}
                </div>
                {selectedTask ? <>
                  <strong className="terminal-task-history-title">{selectedTask.description}</strong>
                  <small className="terminal-task-history-meta">{selectedTask.id} · {taskHistoryLoading ? "正在读取历史日志…" : taskHistoryError ? `历史日志读取失败：${taskHistoryError}` : `${interactionLog[selectedTask.id]?.length ?? 0} 条日志`}</small>
                  <TaskDetail key={selectedTask.id} task={selectedTask} interactionLog={interactionLog[selectedTask.id] ?? []} onNavigateFile={setFileBrowserNav} />
                </> : <p className="terminal-empty-line">当前没有可查看的历史任务</p>}
              </section>
            </div>}
            {view === "workers" && <div className="terminal-data-view"><WorkersPanel workers={workers} tasks={tasks} stale={grpcStale} onJumpTask={(id) => { onSelectTask(id); setView("tasks"); }} /></div>}
            {view === "inference" && <div className="terminal-data-view"><InferencePanel /></div>}
            {view === "logs" && <div className="terminal-data-view"><EventLogPanel events={eventLog} stale={grpcStale} onSelectTask={(id) => { onSelectTask(id); setView("tasks"); }} /></div>}
            {view === "search" && <div className="terminal-data-view"><SearchPanel grpcState={grpcState} onNavigateFile={setFileBrowserNav} stale={grpcStale} /></div>}
            {view === "memory" && <div className="terminal-empty-contract"><span className="terminal-empty-icon">◌</span><h2>Memory surface</h2><p>记忆检索仍由 EngineService 暴露；当前 dashboard Connect 合约未提供独立 Memory RPC。使用 Search 进入真实检索入口。</p><button onClick={() => setView("search")}>打开 Search</button></div>}
          </div>
          <div className={`terminal-notice ${notice.tone}`}><span>❯</span>{notice.text}</div>
        </section>

        {view !== "overview" && <AdvisorRail connected={connected} workers={workers} tasks={tasks} eventLog={eventLog} health={health} metrics={metrics} onView={setView} />}
      </div>

      <StatusPillBar connected={connected} grpcState={grpcState} dashGrpcState={dashGrpcState} workers={workers} tasks={tasks} eventLog={eventLog} />
      <footer className="terminal-command-bar"><span>uc ❯</span><input ref={commandRef} value={command} onChange={(event) => setCommand(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void runCommand(); } }} placeholder='submit "fix flaky heartbeat test"' aria-label="uc 命令行" /><button onClick={() => void runCommand()} disabled={!command.trim() || busy}>运行</button><kbd>↵</kbd><small>运行 · / 聚焦</small><span className="terminal-footer-status">{connected ? "后端在线" : "后端离线"} · engine {health.version ?? "—"} · {lastUpdate ? new Date(lastUpdate).toLocaleTimeString() : "waiting"}</span></footer>
      <div className="terminal-keybar"><span><kbd>Tab</kbd> 面板</span><span><kbd>1</kbd>–<kbd>6</kbd> 视图</span><span><kbd>↑↓</kbd> 主操作</span><span><kbd>⏎</kbd> 执行</span><span><kbd>/</kbd> 命令行</span><span><kbd>?</kbd> 帮助</span><span className="terminal-keybar-right">{connected ? "在线" : "离线"} · dashboard · MIT</span></div>
      <nav className="terminal-mobile-nav" aria-label="移动端视图导航">{VIEWS.map((item) => <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)}><span>{item.icon}</span>{item.label}</button>)}</nav>
      {fileBrowserNav && <div className="terminal-file-browser-fallback"><FileBrowser initialNav={fileBrowserNav} onNavConsumed={() => setFileBrowserNav(null)} stale={grpcStale} /></div>}
      <div className="terminal-connection-diagnostics" aria-label="Connection diagnostics"><ConnectionPill label="Task" state={grpcState} /><ConnectionPill label="Dashboard" state={dashGrpcState} />{grpcExhausted && <button onClick={onReconnectGrpc}>↻ retry gRPC</button>}<button onClick={onReconnectDashGrpc}>↻ retry dashboard</button></div>
      {Object.keys(fetchErrors).length > 0 && <div className="terminal-fetch-warning">⚠ 部分面板不可用：{Object.keys(fetchErrors).join(", ")}</div>}
    </div>
  );
}
