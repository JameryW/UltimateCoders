import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EventLogPanel } from "@/components/panels/EventLogPanel";
import { FileBrowser, type FileBrowserNavigateEvent } from "@/components/panels/FileBrowser";
import { SearchPanel } from "@/components/panels/SearchPanel";
import { TasksPanel } from "@/components/panels/TasksPanel";
import { WorkersPanel } from "@/components/panels/WorkersPanel";
import type { GrpcConnectionState, GrpcSubmitResult, GrpcTaskActionResult } from "@/hooks/useGrpcWeb";
import type { DashboardConnectionState } from "@/hooks/useDashboardGrpc";
import type { Theme } from "@/hooks/useTheme";
import { executeUcCommand } from "@/lib/ucCommands";
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

type TerminalView = "overview" | "tasks" | "workers" | "search" | "memory" | "logs";

const VIEWS: Array<{ id: TerminalView; label: string; meta: string; key: string; icon: string }> = [
  { id: "overview", label: "概览", meta: "CLUSTER", key: "1", icon: "◯" },
  { id: "tasks", label: "任务", meta: "DAG", key: "2", icon: "▤" },
  { id: "workers", label: "工作节点", meta: "WORKERS", key: "3", icon: "⬡" },
  { id: "search", label: "检索", meta: "SEARCH", key: "4", icon: "⌕" },
  { id: "memory", label: "记忆", meta: "MEMORY", key: "5", icon: "◌" },
  { id: "logs", label: "日志", meta: "EVENTS", key: "6", icon: "☷" },
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

function MiniBar({ value, tone = "green" }: { value: number; tone?: "green" | "blue" | "amber" }) {
  return (
    <span className={`terminal-mini-bar ${tone}`}>
      <i style={{ width: `${percent(value)}%` }} />
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

function OverviewView({
  health,
  workers,
  tasks,
  eventLog,
  connected,
  onView,
}: Pick<TerminalDashboardProps, "health" | "workers" | "tasks" | "eventLog" | "connected"> & { onView: (view: TerminalView) => void }) {
  const onlineWorkers = workers.workers.filter((worker) => worker.is_available).length;
  const averageLoad = workers.workers.length
    ? workers.workers.reduce((sum, worker) => sum + percent(worker.load_percent), 0) / workers.workers.length
    : 0;
  const runningTasks = tasks.tasks.filter((task) => ["in_progress", "running", "planning", "assigned"].includes(task.status)).length;
  const eventTotal = eventLog.length;
  const pending = tasks.pending_task_count;
  const queuePercent = tasks.total ? (pending / tasks.total) * 100 : 0;

  return (
    <div className="terminal-overview">
      <div className="terminal-runtime-banner">
        <span>UC / RUNTIME</span>
        <strong>{connected ? "Docker backend connected" : "Waiting for Docker backend"}</strong>
        <small>Health · TaskService · DashboardService · WebSocket TUI</small>
      </div>

      <section className="terminal-topology" aria-label="Cluster topology">
        <div className="terminal-section-head">
          <span>集群拓扑 / CLUSTER TOPOLOGY</span>
          <b><i className={onlineWorkers > 0 ? "terminal-live-dot online" : "terminal-live-dot"} /> {onlineWorkers}/{workers.total} 在线</b>
        </div>
        <button className="terminal-topology-node operator" onClick={() => onView("tasks")}>
          <small>第 0 层 · 操作者</small>
          <strong>UC OPERATOR <em>submit / inspect / control</em></strong>
          <span>真实任务入口 · gRPC-Web + TUI WebSocket</span>
        </button>
        <div className="terminal-topology-arrow">▼</div>
        <button className="terminal-topology-node engine" onClick={() => onView("overview")}>
          <small>第 1 层 · 控制平面</small>
          <strong>UC-ENGINE <em>{health.version ?? "—"}</em> <i className={connected ? "terminal-live-dot online" : "terminal-live-dot"} /> {connected ? "online" : "waiting"}</strong>
          <span>DAG scheduler · DashboardService · live events</span>
        </button>
        <div className="terminal-topology-arrow">▼</div>
        <div className="terminal-worker-lane">
          {workers.workers.length > 0 ? workers.workers.slice(0, 3).map((worker) => (
            <button key={worker.id} className="terminal-worker-node" onClick={() => onView("workers")} title={worker.id}>
              <strong>{shortId(worker.id)}</strong>
              <MiniBar value={worker.load_percent} tone="blue" />
              <b>{percent(worker.load_percent)}%</b>
            </button>
          )) : <div className="terminal-empty-line">后端当前没有注册 worker</div>}
        </div>
        <div className="terminal-topology-arrow">▼</div>
        <div className="terminal-service-lane">
          <div><small>消息总线</small><strong><i className={connected ? "terminal-live-dot online" : "terminal-live-dot"} /> NATS</strong><span>{workers.total} workers</span></div>
          <div><small>检索 / 观测</small><strong>Dashboard</strong><span>{eventTotal} events</span></div>
        </div>
      </section>

      <div className="terminal-runtime-checks">
        <div><b>» engine · {connected ? "● connected" : "○ disconnected"}</b><span>{health.status || "waiting"}</span></div>
        <div><b>» workers · {workers.total} registered</b><span>{onlineWorkers} available</span></div>
        <div><b>» DAG · {tasks.total} tasks</b><span>{runningTasks} running</span></div>
        <div><b>» events · {eventTotal} buffered</b><span>real backend events</span></div>
      </div>

      <div className="terminal-stat-grid">
        <div><b>{formatUptime(health.uptime_seconds)}</b><span>运行时长 · hh:mm:ss</span></div>
        <div><b>{tasks.total}</b><span>DAG 任务</span></div>
        <div><b>{runningTasks}</b><span>运行中</span></div>
        <div><b>{eventTotal}</b><span>事件缓冲</span></div>
      </div>

      <section className="terminal-vitals">
        <div className="terminal-vitals-head">运行体征 · REALTIME <i className={connected ? "terminal-live-dot online" : "terminal-live-dot"} /></div>
        <div className="terminal-vital-row"><span>workers · load</span><MiniBar value={averageLoad} /><strong>{percent(averageLoad)}%</strong></div>
        <div className="terminal-vital-row"><span>engine · queue</span><MiniBar value={queuePercent} tone="amber" /><strong>{pending}</strong></div>
        <div className="terminal-vital-row"><span>events · buffer</span><MiniBar value={Math.min(100, eventTotal)} tone="blue" /><strong>{eventTotal}</strong></div>
      </section>

      <p className="terminal-help-line">提示：顶栏 <kbd>1</kbd>–<kbd>6</kbd> 切视图 · 左栏主操作 · <kbd>Ctrl</kbd><kbd>K</kbd> 聚焦命令 · <kbd>/</kbd> 命令行</p>
    </div>
  );
}

function ClusterRail({
  connected,
  workers,
  tasks,
  eventLog,
  health,
  metrics,
  onView,
}: Pick<TerminalDashboardProps, "connected" | "workers" | "tasks" | "eventLog" | "health" | "metrics"> & { onView: (view: TerminalView) => void }) {
  const onlineWorkers = workers.workers.filter((worker) => worker.is_available).length;
  const load = workers.workers.length ? workers.workers.reduce((sum, worker) => sum + worker.load_percent, 0) / workers.workers.length : 0;
  const latestEvents = eventLog.slice(-5).reverse();
  return (
    <aside className="terminal-cluster-rail">
      <div className="terminal-rail-head"><span>集群 / CLUSTER</span><b className={connected ? "online" : "offline"}><i /> {connected ? "ONLINE" : "OFFLINE"}</b></div>
      <div className="terminal-cluster-summary">
        <div><b>{onlineWorkers}<small>/{workers.total}</small></b><span>workers online</span></div>
        <div><b>{tasks.total}</b><span>dag tasks</span></div>
        <div><b>{eventLog.length}</b><span>events</span></div>
      </div>
      <section className="terminal-rail-section">
        <div className="terminal-rail-section-head"><span>运行负载</span><button onClick={() => onView("workers")}>VIEW</button></div>
        <div className="terminal-rail-metric"><span>cluster load</span><b>{percent(load)}%</b></div>
        <MiniBar value={load} tone="blue" />
        <div className="terminal-rail-metric"><span>queue</span><b>{tasks.pending_task_count}</b></div>
        <MiniBar value={tasks.total ? (tasks.pending_task_count / tasks.total) * 100 : 0} tone="amber" />
      </section>
      <section className="terminal-rail-section">
        <div className="terminal-rail-section-head"><span>后端组件</span><button onClick={() => onView("overview")}>HEALTH</button></div>
        <ConnectionPill label="engine" state={connected ? "connected" : "disconnected"} />
        <ConnectionPill label="workers" state={workers.available ? "connected" : "disconnected"} />
        <ConnectionPill label="health" state={health.available ? "connected" : "disconnected"} />
      </section>
      <section className="terminal-rail-section terminal-event-feed">
        <div className="terminal-rail-section-head"><span>事件流</span><button onClick={() => onView("logs")}>ALL</button></div>
        {latestEvents.length === 0 ? <div className="terminal-empty-line">等待真实事件…</div> : latestEvents.map((event, index) => (
          <button key={`${event.timestamp}-${event.type}-${index}`} className="terminal-event-row" onClick={() => onView("logs")}>
            <i /> <span>{event.type}</span><time>{new Date(event.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>
          </button>
        ))}
      </section>
      <section className="terminal-rail-section terminal-health-meter">
        <div className="terminal-rail-section-head"><span>系统体征</span><span className="terminal-accent-text">{(metrics?.system.cluster_utilization_pct ?? 0).toFixed(0)}%</span></div>
        <MiniBar value={metrics?.system.cluster_utilization_pct ?? 0} />
        <small>{health.version ? `engine ${health.version}` : "engine version unavailable"}</small>
      </section>
    </aside>
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
  const [notice, setNotice] = useState({ text: "uc-tui 就绪 — 正在读取真实 Docker Gateway", tone: "info" });
  const [clock, setClock] = useState(() => new Date());
  const [busy, setBusy] = useState(false);
  const [fileBrowserNav, setFileBrowserNav] = useState<FileBrowserNavigateEvent | null>(null);
  const commandRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

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
    <div className="terminal-app" data-theme={theme}>
      <header className="terminal-topbar">
        <div className="terminal-window-dots" aria-hidden="true"><i className="red" /><i className="yellow" /><i className="green" /></div>
        <div className={`terminal-brand ${connected ? "online" : "offline"}`}><span>ULTIMATE</span><strong>CODERS</strong></div>
        <div className="terminal-brand-tag">distributed ai coding system · uc-tui</div>
        <nav className="terminal-tabs" aria-label="视图切换">
          <span className="terminal-tabs-label">视图</span>
          {VIEWS.map((item) => <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)}><b>{item.key}</b>{item.label}<small>{item.meta}</small></button>)}
        </nav>
        <div className="terminal-topbar-status"><i className={connected ? "online" : ""} />{connected ? "NATS" : "OFFLINE"}<small>{workers.total} workers</small><time>{formatClock(clock)}</time></div>
        <div className="terminal-topbar-connections"><ConnectionPill label="gRPC" state={grpcState} /><ConnectionPill label="Dash" state={dashGrpcState} /></div>
        <a className="terminal-tui-entry" href="#/tui" aria-label="打开 TUI"><span>❯</span><small>TUI</small></a>
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
            {view === "overview" && <OverviewView health={health} workers={workers} tasks={tasks} eventLog={eventLog} connected={connected} onView={setView} />}
            {view === "tasks" && <div className="terminal-data-view"><TasksPanel data={tasks} interactionLog={interactionLog} onFlush={onFlush} onPauseTask={onPauseTask} onResumeTask={onResumeTask} onCancelTask={onCancelTask} stale={grpcStale} grpcSubmitTask={grpcSubmitTask} onTaskCreated={onTaskCreated} onOptimisticAdd={onOptimisticAdd} onSelectTask={onSelectTask} selectedTaskId={selectedTaskId} onNavigateFile={setFileBrowserNav} /></div>}
            {view === "workers" && <div className="terminal-data-view"><WorkersPanel workers={workers} tasks={tasks} stale={grpcStale} onJumpTask={onSelectTask} /></div>}
            {view === "logs" && <div className="terminal-data-view"><EventLogPanel events={eventLog} stale={grpcStale} onSelectTask={onSelectTask} /></div>}
            {view === "search" && <div className="terminal-data-view"><SearchPanel grpcState={grpcState} onNavigateFile={setFileBrowserNav} stale={grpcStale} /></div>}
            {view === "memory" && <div className="terminal-empty-contract"><span className="terminal-empty-icon">◌</span><h2>Memory surface</h2><p>记忆检索仍由 EngineService 暴露；当前 dashboard Connect 合约未提供独立 Memory RPC。使用 TUI 或 Search 进入真实检索入口。</p><button onClick={() => setView("search")}>打开 Search</button></div>}
          </div>
          <div className={`terminal-notice ${notice.tone}`}><span>❯</span>{notice.text}</div>
        </section>

        <ClusterRail connected={connected} workers={workers} tasks={tasks} eventLog={eventLog} health={health} metrics={metrics} onView={setView} />
      </div>

      <footer className="terminal-command-bar"><span>uc ❯</span><input ref={commandRef} value={command} onChange={(event) => setCommand(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void runCommand(); } }} placeholder='submit "fix flaky heartbeat test"' aria-label="uc 命令行" /><button onClick={() => void runCommand()} disabled={!command.trim() || busy}>运行</button><kbd>↵</kbd><small>运行 · / 聚焦</small><span className="terminal-footer-status">{connected ? "后端在线" : "后端离线"} · engine {health.version ?? "—"} · {lastUpdate ? new Date(lastUpdate).toLocaleTimeString() : "waiting"}</span></footer>
      <div className="terminal-keybar"><span><kbd>Tab</kbd> 面板</span><span><kbd>1</kbd>–<kbd>6</kbd> 视图</span><span><kbd>↑↓</kbd> 主操作</span><span><kbd>⏎</kbd> 执行</span><span><kbd>/</kbd> 命令行</span><span><kbd>?</kbd> 帮助</span><span className="terminal-keybar-right">{connected ? "在线" : "离线"} · uc-tui · MIT</span></div>
      <nav className="terminal-mobile-nav" aria-label="移动端视图导航">{VIEWS.map((item) => <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)}><span>{item.icon}</span>{item.label}</button>)}</nav>
      {fileBrowserNav && <div className="terminal-file-browser-fallback"><FileBrowser initialNav={fileBrowserNav} onNavConsumed={() => setFileBrowserNav(null)} stale={grpcStale} /></div>}
      <div className="terminal-connection-diagnostics" aria-label="Connection diagnostics"><ConnectionPill label="Task" state={grpcState} /><ConnectionPill label="Dashboard" state={dashGrpcState} />{grpcExhausted && <button onClick={onReconnectGrpc}>↻ retry gRPC</button>}<button onClick={onReconnectDashGrpc}>↻ retry dashboard</button></div>
      {Object.keys(fetchErrors).length > 0 && <div className="terminal-fetch-warning">⚠ 部分面板不可用：{Object.keys(fetchErrors).join(", ")}</div>}
    </div>
  );
}
