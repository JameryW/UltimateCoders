import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EventLogPanel } from "@/components/panels/EventLogPanel";
import type { FileBrowserNavigateEvent } from "@/components/panels/FileBrowser";
import { SearchPanel } from "@/components/panels/SearchPanel";
import { TaskDetail } from "@/components/panels/TaskDetail";
import { TasksPanel } from "@/components/panels/TasksPanel";
import { WorkersPanel } from "@/components/panels/WorkersPanel";
import { InferencePanel } from "@/components/panels/InferencePanel";
import type { GrpcConnectionState, GrpcSubmitResult, GrpcTaskActionResult } from "@/hooks/useGrpcWeb";
import type { DashboardConnectionState } from "@/hooks/useDashboardGrpc";
import type { Theme } from "@/hooks/useTheme";
import { executeUcCommand } from "@/lib/ucCommands";
import { hashForView, viewFromHash, type TerminalView } from "@/lib/terminalView";
import { CommandPalette, type PaletteCommand } from "@/components/terminal/CommandPalette";

// FileBrowser pulls in highlight.js, so it loads on demand the first time the
// search panel navigates to a file instead of riding in the dashboard bundle.
const FileBrowser = lazy(() =>
  import("@/components/panels/FileBrowser").then((module) => ({ default: module.FileBrowser })),
);
import { latestDashboardEvents } from "@/lib/dashboardEvents";
import { aggregateSubtaskProgress, subtaskShortLabel } from "@/lib/taskProgress";
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

const VIEWS: Array<{ id: TerminalView; label: string; meta: string; key: string; icon: string }> = [
  { id: "overview", label: "概览", meta: "CLUSTER", key: "1", icon: "◯" },
  { id: "tasks", label: "任务", meta: "DAG", key: "2", icon: "▤" },
  { id: "workers", label: "工作节点", meta: "WORKERS", key: "3", icon: "⬡" },
  { id: "search", label: "检索", meta: "SEARCH", key: "4", icon: "⌕" },
  { id: "memory", label: "记忆", meta: "MEMORY", key: "5", icon: "◌" },
  { id: "logs", label: "日志", meta: "EVENTS", key: "6", icon: "☷" },
  { id: "inference", label: "推理实验", meta: "INFERENCE", key: "7", icon: "◈" },
];

/** Tasks still moving through the pipeline — listed before finished ones. */
function isLiveTask(task: { status: string }): boolean {
  return task.status === "inprogress" || task.status === "pending" || task.status === "paused";
}

/** Single-letter view shortcuts (shown in the "?" help sheet). */
const LETTER_VIEWS: Record<string, TerminalView> = {
  o: "overview",
  t: "tasks",
  w: "workers",
  s: "search",
  m: "memory",
  e: "logs",
  i: "inference",
};

interface TerminalDashboardProps {
  connected: boolean;
  grpcState: GrpcConnectionState;
  grpcExhausted: boolean;
  dashGrpcState: DashboardConnectionState;
  lastUpdate?: string;
  theme: Theme;
  onToggleTheme: () => void;
  /** Optional explicit setter — used for first-visit dark default without flipping a stored light preference. */
  onSetTheme?: (theme: Theme) => void;
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
  /** Optional initial tab — defaults to overview (command-center first paint). */
  initialView?: TerminalView;
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

/**
 * Format wall-clock elapsed since an ISO timestamp as `HH:MM:SS`.
 * Invalid / missing `fromIso` → `—`. Negative deltas clamp to `00:00:00`.
 */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function formatElapsed(fromIso: string | null | undefined, nowMs: number): string {
  if (!fromIso) return "—";
  const start = Date.parse(fromIso);
  if (!Number.isFinite(start)) return "—";
  const elapsedSec = Math.max(0, Math.floor((nowMs - start) / 1000));
  return formatUptime(elapsedSec);
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

function TaskHistoryLiveStrip({ task }: { task: TaskSummary }) {
  const live = aggregateSubtaskProgress(task.subtasks);
  if (live.total === 0) return null;
  return (
    <div className="terminal-task-live-strip" data-testid="terminal-task-live-strip">
      <span className="terminal-task-live-count">{live.completed}/{live.total}</span>
      <MiniBar value={live.percent} tone="blue" />
      <b className="terminal-task-live-pct">{live.percent}%</b>
      {(live.phase || live.stepAgent) && (
        <span className="terminal-task-live-phase" data-testid="terminal-task-live-phase">
          {live.phase || "running"}
          {live.stepAgent ? ` · ${live.stepAgent}` : ""}
        </span>
      )}
    </div>
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

/**
 * Auto-select the first active/running task when overview has no selection.
 * Returns null when the user already selected a task (never overrides).
 */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function pickAutofocusTaskId(
  tasks: Pick<TasksData, "tasks">,
  selectedTaskId: string | null | undefined,
): string | null {
  if (selectedTaskId) return null;
  const list = tasks.tasks ?? [];
  const active = list.find((task) => ACTIVE_TASK_STATUSES.has(task.status));
  return active?.id ?? null;
}

const THEME_STORAGE_KEY = "uc_dashboard_theme";

/**
 * Prefer dark command-center chrome on first visit only.
 * If `uc_dashboard_theme` is already set (user chose light or dark), return false
 * so we never fight an explicit preference — including an explicit light theme.
 */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function shouldApplyCommandCenterDefaultDark(
  theme: Theme,
  readStored: (key: string) => string | null = (key) => {
    try {
      return localStorage.getItem(key);
    } catch {
      // Fail closed: treat storage as having a preference so we do not override.
      return "dark";
    }
  },
): boolean {
  if (readStored(THEME_STORAGE_KEY) !== null) return false;
  return theme === "light";
}

function isSubtaskDone(subtask: NonNullable<TaskSummary["subtasks"]>[number]): boolean {
  return DONE_SUBTASK_STATUSES.has(subtask.status)
    || subtask.step_status === "completed"
    || percent(subtask.percent ?? 0) >= 100;
}

function isSubtaskRunning(subtask: NonNullable<TaskSummary["subtasks"]>[number]): boolean {
  if (isSubtaskDone(subtask)) return false;
  return RUNNING_SUBTASK_STATUSES.has(subtask.status)
    || subtask.step_status === "started"
    || (typeof subtask.percent === "number" && percent(subtask.percent) > 0 && percent(subtask.percent) < 100);
}

function collectSwarmCards(lead: TaskSummary | null, workers: WorkersData) {
  // Prefer live running subtasks; otherwise fall back to workers (not completed %).
  const runningSubtasks = (lead?.subtasks ?? [])
    .filter(isSubtaskRunning)
    .slice(0, 3)
    .map((subtask) => ({
      id: subtask.id,
      title: subtaskShortLabel(subtask.id),
      detail: subtask.phase || subtask.step_summary || subtask.description || "subtask",
      value: percent(subtask.percent ?? 35),
      done: false,
      running: true,
    }));

  if (runningSubtasks.length > 0) return runningSubtasks;

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

/** Live command-center stage driven by UC runtime state (not a fake idle loop). */
export type CommandCenterStage = "idle" | "lead" | "dispatch" | "swarm" | "review";

const STAGE_ORDER: CommandCenterStage[] = ["idle", "lead", "dispatch", "swarm", "review"];
/** Start on swarm so connector particles + stage glow are visible in the offline sample. */
const PREVIEW_STAGE_CYCLE: CommandCenterStage[] = ["swarm", "dispatch", "swarm", "review", "lead"];

export const COMMAND_CENTER_IDLE_TIP = "» submit a task to see live flow";
export const COMMAND_CENTER_LIVE_HANDOFF = "live · gateway connected";

/**
 * Map active stage chip → dashboard view.
 * swarm prefers tasks when the lead has running subtasks; otherwise workers.
 * idle → null (chip is non-navigating).
 */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function viewForCommandCenterStage(
  stage: CommandCenterStage,
  hasRunningSubtasks = false,
): "tasks" | "workers" | null {
  if (stage === "lead" || stage === "review") return "tasks";
  if (stage === "dispatch") return "workers";
  if (stage === "swarm") return hasRunningSubtasks ? "tasks" : "workers";
  return null;
}

type SwarmCard = {
  id: string;
  title: string;
  detail: string;
  value: number;
  done: boolean;
  running: boolean;
};

export type StatusPillValues = {
  workers: string;
  tasks: string;
  queue: string;
  grpc: string;
  dash: string;
  events: string;
  backend: string;
};

/** Diff pill snapshots so StatusPillBar can flash only changed keys. */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function changedPillKeys(prev: StatusPillValues | null, next: StatusPillValues): Array<keyof StatusPillValues> {
  if (!prev) return [];
  return (Object.keys(next) as Array<keyof StatusPillValues>).filter((key) => prev[key] !== next[key]);
}

function taskEventToDashboardEvent(event: TaskEvent): DashboardEvent {
  return {
    timestamp: event.timestamp,
    type: event.type,
    details: {
      task_id: event.task_id,
      ...(event.subtask_id ? { subtask_id: event.subtask_id } : {}),
      ...(event.data ?? {}),
    },
  };
}

/** Stable identity for session rows — no list index (indexes shift when newer events prepend). */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function sessionLogEventKey(event: DashboardEvent): string {
  const taskId = typeof event.details?.task_id === "string" ? event.details.task_id : "";
  const subtaskId = typeof event.details?.subtask_id === "string" ? event.details.subtask_id : "";
  return `${event.timestamp}|${event.type}|${taskId}|${subtaskId}`;
}

/** Merge dashboard eventLog with selected/lead interaction events (newest first, capped). */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function mergeSessionLogEvents(
  eventLog: DashboardEvent[],
  interactionEvents: TaskEvent[],
  limit = 8,
): DashboardEvent[] {
  const merged = [
    ...(eventLog ?? []),
    ...(interactionEvents ?? []).map(taskEventToDashboardEvent),
  ];
  merged.sort((a, b) => {
    const ta = Date.parse(a.timestamp) || 0;
    const tb = Date.parse(b.timestamp) || 0;
    return tb - ta;
  });
  const seen = new Set<string>();
  const unique: DashboardEvent[] = [];
  for (const event of merged) {
    const key = sessionLogEventKey(event);
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(event);
    if (unique.length >= limit) break;
  }
  return unique;
}

/** Connected + empty/idle board (not SAMPLE preview) — show quiet submit tip. */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function isCommandCenterConnectedIdle(input: {
  connected: boolean;
  previewMode: boolean;
  stage: CommandCenterStage;
}): boolean {
  return input.connected && !input.previewMode && input.stage === "idle";
}

/** Offline + empty cluster → show unmistakably-labeled sample board (not production data). */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested preview gate
export function shouldUseCommandCenterPreview(input: {
  connected: boolean;
  tasks: Pick<TasksData, "tasks" | "total">;
  workers: Pick<WorkersData, "workers" | "total">;
}): boolean {
  if (input.connected) return false;
  const taskCount = input.tasks.tasks?.length ?? 0;
  const workerCount = input.workers.workers?.length ?? 0;
  if (taskCount > 0 || workerCount > 0) return false;
  if ((input.tasks.total ?? 0) > 0 || (input.workers.total ?? 0) > 0) return false;
  return true;
}

const PREVIEW_LEAD_DESCRIPTION = "Ship command-center live board";

function buildPreviewBoard(tick: number): {
  lead: TaskSummary;
  pending: number;
  averageLoad: number;
  onlineWorkers: number;
  workerTotal: number;
  onlineRatio: number;
  queuePercent: number;
  runningTasks: number;
  swarm: SwarmCard[];
  review: { title: string; detail: string };
  sessionEvents: DashboardEvent[];
  health: HealthData;
  advice: DashboardEvent[];
  stage: CommandCenterStage;
} {
  const phase = PREVIEW_STAGE_CYCLE[tick % PREVIEW_STAGE_CYCLE.length] ?? "swarm";
  const drift = (tick % 5) * 3;
  const swarm: SwarmCard[] = [
    {
      id: "preview-swarm-a",
      title: "preview-a",
      detail: "wire advisor rail",
      value: Math.min(98, 92 + (tick % 3)),
      done: true,
      running: false,
    },
    {
      id: "preview-swarm-b",
      title: "preview-b",
      detail: "dispatch bars",
      value: percent(48 + drift),
      done: false,
      running: true,
    },
    {
      id: "preview-swarm-c",
      title: "preview-c",
      detail: "session log polish",
      value: percent(31 + drift),
      done: false,
      running: true,
    },
  ];
  const lead: TaskSummary = {
    id: "preview-task",
    description: PREVIEW_LEAD_DESCRIPTION,
    status: phase === "review" ? "completed" : "in_progress",
    project_id: "preview",
    subtask_count: 3,
    created_at: "2026-10-31T00:00:00Z",
    updated_at: "2026-10-31T00:01:00Z",
    subtasks: swarm.map((card) => ({
      id: card.id,
      description: card.detail,
      status: card.done ? "completed" : "in_progress",
      depends_on: [],
      percent: card.value,
      phase: card.detail,
      step_status: card.done ? "completed" : "started",
    })),
  };
  const sessionEvents: DashboardEvent[] = [
    { timestamp: "2026-10-31T12:04:18Z", type: "subtask_progress", details: { task_id: lead.id, percent: swarm[1]?.value } },
    { timestamp: "2026-10-31T12:04:11Z", type: "subtask_started", details: { task_id: lead.id, worker_id: "preview-b" } },
    { timestamp: "2026-10-31T12:03:58Z", type: "task_submitted", details: { task_id: lead.id, description: PREVIEW_LEAD_DESCRIPTION } },
    { timestamp: "2026-10-31T12:03:40Z", type: "health_check", details: { status: "waiting", note: "gateway offline" } },
  ];
  return {
    lead,
    pending: 2,
    averageLoad: 48,
    onlineWorkers: 2,
    workerTotal: 3,
    onlineRatio: (2 / 3) * 100,
    queuePercent: 40,
    runningTasks: phase === "review" ? 0 : 1,
    swarm,
    review: phase === "review"
      ? { title: "review · completed", detail: "verify outputs · inspect history" }
      : { title: "review · in flight", detail: "awaiting verify after swarm completes" },
    sessionEvents,
    health: {
      available: false,
      status: "waiting",
      version: "preview",
      uptime_seconds: 0,
      components: [
        { name: "gateway", status: "offline" },
        { name: "nats", status: "idle" },
        { name: "workers", status: "sample" },
        { name: "memory", status: "idle" },
      ],
    },
    advice: sessionEvents.slice(0, 3),
    stage: phase,
  };
}

// eslint-disable-next-line react-refresh/only-export-components -- unit-tested stage helper shared with OverviewView
export function deriveActiveStage(input: {
  connected: boolean;
  lead: TaskSummary | null;
  selectedTask: TaskSummary | null;
  pending: number;
  swarm: Array<{ running: boolean }>;
}): CommandCenterStage {
  const { connected, lead, selectedTask, pending, swarm } = input;
  const focus = selectedTask ?? lead;
  const hasActiveTask = Boolean(lead && ACTIVE_TASK_STATUSES.has(lead.status));
  const hasRunningSwarm = swarm.some((card) => card.running)
    || (lead?.subtasks ?? []).some(isSubtaskRunning);

  // Furthest-advanced hot stage wins when multiple conditions apply.
  if (focus && TERMINAL_TASK_STATUSES.has(focus.status)) return "review";
  if (hasRunningSwarm) return "swarm";
  if (pending > 0 || (hasActiveTask && lead && (lead.status === "in_progress" || lead.status === "running") && !hasRunningSwarm)) {
    return "dispatch";
  }
  if (hasActiveTask) return "lead";
  if (!connected) return "idle";
  return "idle";
}

function connectorFlowsFor(stage: CommandCenterStage): { afterLead: boolean; afterDispatch: boolean; afterSwarm: boolean } {
  const index = STAGE_ORDER.indexOf(stage);
  // Connectors above the active stage carry traveling light.
  return {
    afterLead: index >= STAGE_ORDER.indexOf("dispatch"),
    afterDispatch: index >= STAGE_ORDER.indexOf("swarm"),
    afterSwarm: index >= STAGE_ORDER.indexOf("review"),
  };
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
  preview = false,
}: Pick<TerminalDashboardProps, "connected" | "workers" | "tasks" | "eventLog" | "health" | "metrics"> & {
  onView: (view: TerminalView) => void;
  compact?: boolean;
  preview?: boolean;
}) {
  const workerList = workers.workers ?? [];
  const onlineWorkers = workerList.filter((worker) => worker.is_available).length;
  const advice = latestDashboardEvents(eventLog ?? [], 4);
  const components = (health.components ?? []).slice(0, 4);
  const focusIndex = Math.max(0, components.findIndex((component) => {
    const status = component.status.toLowerCase();
    return status !== "ok" && status !== "healthy" && status !== "idle";
  }));
  const allComponentsHealthy = components.length > 0 && components.every((component) => {
    const status = component.status.toLowerCase();
    return status === "ok" || status === "healthy" || status === "idle";
  });
  const statusHealthy = health.status === "ok" || health.status === "healthy";
  const healthLabel = statusHealthy || allComponentsHealthy ? "healthy" : health.status || "unknown";
  const phase = preview
    ? "preview sample · waiting for gateway"
    : !connected
      ? "waiting for gateway"
      : statusHealthy || allComponentsHealthy
        ? "cluster healthy"
        : health.status || "checking health";

  return (
    <aside className={`cc-advisor ${compact ? "compact" : ""}${preview ? " preview" : ""}`} aria-label="Health advisor">
      <div className="cc-advisor-head">
        <span>HEALTH advisor · on call</span>
        <b className={connected && !preview ? "online" : "offline"}>{preview ? "preview" : connected ? "live" : "offline"}</b>
      </div>
      <div className="cc-advisor-phase"><span className="cc-diamond">◆</span> {phase}</div>
      <div className="cc-advisor-section">
        <div className="cc-advisor-label">components</div>
        {components.length === 0 ? (
          <div className="cc-advisor-empty">no health components yet</div>
        ) : components.map((component, index) => (
          <div key={component.name} className={`cc-advisor-line${index === focusIndex ? " focus" : ""}`}>
            <span>{index === focusIndex ? "◆" : "◇"} {component.name}</span>
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
        <span>{preview ? "◆" : "◇"} {healthLabel}</span>
        <button onClick={() => onView("logs")}>session log</button>
      </div>
    </aside>
  );
}

function formatSessionStamp(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

const SESSION_DETAIL_SKIP = new Set(["task_id", "subtask_id"]);

function sessionLogLine(event: DashboardEvent): string {
  const type = event.type.replace(/_/g, " ");
  const details = event.details ?? {};
  const detailKeys = Object.keys(details);
  // Prefer payload keys (tool/worker/percent) over id fields for the two-slot preview.
  const preferred = [
    ...detailKeys.filter((key) => !SESSION_DETAIL_SKIP.has(key)),
    ...detailKeys.filter((key) => SESSION_DETAIL_SKIP.has(key)),
  ].slice(0, 2);
  let detail = "";
  if (preferred.length > 0) {
    detail = preferred.map((key) => {
      const value = details[key];
      if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
        return `${key}=${String(value).slice(0, 28)}`;
      }
      return key;
    }).join(" · ");
  }
  return detail ? `${type} → ${detail}` : type;
}

function OverviewView({
  health,
  workers,
  tasks,
  eventLog,
  metrics,
  connected,
  selectedTask,
  interactionLog,
  onView,
  onSelectTask,
}: Pick<TerminalDashboardProps, "health" | "workers" | "tasks" | "eventLog" | "metrics" | "connected" | "selectedTask" | "interactionLog"> & {
  onView: (view: TerminalView) => void;
  onSelectTask: (taskId: string | null) => void;
}) {
  const previewMode = shouldUseCommandCenterPreview({ connected, tasks, workers });
  // Tick only advances while previewMode is on; leaving preview stops the timer
  // without a sync setState reset (react-hooks/set-state-in-effect).
  const [previewTick, setPreviewTick] = useState(0);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [showLiveHandoff, setShowLiveHandoff] = useState(false);
  const wasPreviewRef = useRef(previewMode);
  const knownSessionKeysRef = useRef<Set<string> | null>(null);
  const [enteringSessionKeys, setEnteringSessionKeys] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    if (!previewMode || typeof window === "undefined") return;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    if (reduced) return;
    const timer = window.setInterval(() => setPreviewTick((tick) => tick + 1), 3200);
    return () => window.clearInterval(timer);
  }, [previewMode]);

  // Lead elapsed clock — 1s while OverviewView is mounted.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  // Preview → live handoff: brief notice when SAMPLE flips off (gateway/data arrived).
  // Keep wasPreviewRef true until the handoff window finishes so React Strict Mode
  // effect cleanup+rerun cannot swallow the true→false edge.
  useEffect(() => {
    if (previewMode) {
      wasPreviewRef.current = true;
      const hideTimer = window.setTimeout(() => setShowLiveHandoff(false), 0);
      return () => window.clearTimeout(hideTimer);
    }
    if (!wasPreviewRef.current) return;
    const startTimer = window.setTimeout(() => setShowLiveHandoff(true), 0);
    const clearTimer = window.setTimeout(() => {
      setShowLiveHandoff(false);
      wasPreviewRef.current = false;
    }, 2000);
    return () => {
      window.clearTimeout(startTimer);
      window.clearTimeout(clearTimer);
    };
  }, [previewMode]);

  const preview = previewMode ? buildPreviewBoard(previewTick) : null;

  const workerList = workers.workers ?? [];
  const liveOnlineWorkers = workerList.filter((worker) => worker.is_available).length;
  const liveAverageLoad = workerList.length
    ? workerList.reduce((sum, worker) => sum + percent(worker.load_percent), 0) / workerList.length
    : 0;
  const liveRunningTasks = (tasks.tasks ?? []).filter((task) => ACTIVE_TASK_STATUSES.has(task.status)).length;
  const livePending = tasks.pending_task_count ?? 0;
  const liveOnlineRatio = workers.total ? (liveOnlineWorkers / workers.total) * 100 : 0;
  const liveQueuePercent = tasks.total
    ? Math.min(100, (livePending / Math.max(tasks.total, 1)) * 100)
    : livePending > 0 ? 100 : 0;
  const liveLead = pickLeadTask(tasks, selectedTask);
  const liveSwarm = collectSwarmCards(liveLead, workers);
  const liveReview = reviewCue(liveLead, selectedTask);
  const focusTaskId = selectedTask?.id ?? liveLead?.id;
  const interactionEvents = focusTaskId ? (interactionLog?.[focusTaskId] ?? []) : [];
  const liveSessionEvents = mergeSessionLogEvents(eventLog ?? [], interactionEvents, 8);
  const liveStage = deriveActiveStage({
    connected,
    lead: liveLead,
    selectedTask,
    pending: livePending,
    swarm: liveSwarm,
  });

  const lead = preview?.lead ?? liveLead;
  const swarm = preview?.swarm ?? liveSwarm;
  const review = preview?.review ?? liveReview;
  const sessionEvents = preview?.sessionEvents ?? liveSessionEvents;
  const pending = preview?.pending ?? livePending;
  const averageLoad = preview?.averageLoad ?? liveAverageLoad;
  const onlineWorkers = preview?.onlineWorkers ?? liveOnlineWorkers;
  const workerTotal = preview?.workerTotal ?? workers.total;
  const onlineRatio = preview?.onlineRatio ?? liveOnlineRatio;
  const queuePercent = preview?.queuePercent ?? liveQueuePercent;
  const runningTasks = preview?.runningTasks ?? liveRunningTasks;
  const activeStage = preview?.stage ?? liveStage;
  const showIdleTip = isCommandCenterConnectedIdle({
    connected,
    previewMode,
    stage: activeStage,
  });
  const hasRunningSubtasks = (lead?.subtasks ?? []).some(isSubtaskRunning);
  const stageNavView = viewForCommandCenterStage(activeStage, hasRunningSubtasks);
  const leadElapsed = lead
    ? formatElapsed(lead.updated_at || lead.created_at, nowMs)
    : "—";
  const flow = connectorFlowsFor(activeStage);
  const boardHealth = preview?.health ?? health;
  const boardEvents = preview?.advice ?? eventLog;
  const boardWorkers: WorkersData = preview
    ? {
        available: false,
        total: preview.workerTotal,
        available_count: preview.onlineWorkers,
        workers: Array.from({ length: preview.workerTotal }, (_, index) => ({
          id: `preview-worker-${index + 1}`,
          capabilities: ["code"],
          current_load: index < preview.onlineWorkers ? 1 : 0,
          max_capacity: 2,
          load_percent: index < preview.onlineWorkers ? preview.averageLoad : 0,
          last_heartbeat: "2026-10-31T12:00:00Z",
          heartbeat_age_seconds: 0,
          heartbeat_stale: false,
          is_available: index < preview.onlineWorkers,
        })),
      }
    : workers;
  const boardTasks: TasksData = preview
    ? {
        available: false,
        tasks: [preview.lead],
        total: 1,
        status_counts: { in_progress: 1 },
        pending_task_count: preview.pending,
      }
    : tasks;

  const sessionKeys = sessionEvents.map((event) => sessionLogEventKey(event));
  const sessionKeySignature = sessionKeys.join("|");

  useEffect(() => {
    // Defer setState into timers so we never sync-setState in the effect body
    // (react-hooks/set-state-in-effect). Keys are identity-stable — no index.
    if (previewMode) {
      knownSessionKeysRef.current = new Set(sessionKeys);
      const clearTimer = window.setTimeout(() => {
        setEnteringSessionKeys((prev) => (prev.size === 0 ? prev : new Set()));
      }, 0);
      return () => window.clearTimeout(clearTimer);
    }
    const known = knownSessionKeysRef.current;
    if (!known) {
      knownSessionKeysRef.current = new Set(sessionKeys);
      return;
    }
    const fresh = sessionKeys.filter((key) => !known.has(key));
    knownSessionKeysRef.current = new Set(sessionKeys);
    if (fresh.length === 0) return;
    const startTimer = window.setTimeout(() => setEnteringSessionKeys(new Set(fresh)), 0);
    const clearTimer = window.setTimeout(() => setEnteringSessionKeys(new Set()), 520);
    return () => {
      window.clearTimeout(startTimer);
      window.clearTimeout(clearTimer);
    };
    // sessionKeys is mirrored by sessionKeySignature for eslint-static deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- signature tracks key identity joins
  }, [previewMode, sessionKeySignature]);

  return (
    <div className={`terminal-overview cc-board${previewMode ? " cc-preview" : ""}`} data-cc-preview={previewMode ? "sample" : "live"}>
      {previewMode && (
        <div className="cc-preview-banner" role="status" data-testid="cc-preview-banner">
          <strong>SAMPLE</strong>
          <span>preview · not live data — waiting for gateway</span>
        </div>
      )}
      {showLiveHandoff && !previewMode && (
        <div className="cc-live-banner" role="status" data-testid="cc-live-banner">
          <strong>LIVE</strong>
          <span>{COMMAND_CENTER_LIVE_HANDOFF}</span>
        </div>
      )}
      <div className="cc-legend-row">
        <RoleLegend />
        <button
          type="button"
          className={`cc-stage-chip ${activeStage}`}
          data-testid="cc-stage-chip"
          data-stage={activeStage}
          aria-label={`stage · ${activeStage}${stageNavView ? ` · open ${stageNavView}` : ""}`}
          disabled={!stageNavView}
          onClick={() => {
            if (stageNavView) onView(stageNavView);
          }}
        >
          stage · {activeStage}
        </button>
      </div>
      <div className="cc-layout">
        <AdvisorPanel
          connected={connected}
          workers={boardWorkers}
          tasks={boardTasks}
          eventLog={boardEvents}
          health={boardHealth}
          metrics={previewMode ? null : metrics}
          onView={onView}
          preview={previewMode}
        />

        <section
          className={`cc-flow stage-${activeStage}${previewMode ? " cc-preview-flow" : ""}`}
          data-active-stage={activeStage}
          aria-label={previewMode ? "Preview execution flow" : "Live execution flow"}
        >
          <button
            className={`cc-card lead${activeStage === "lead" ? " cc-stage-active" : ""}`}
            data-stage="lead"
            onClick={() => {
              if (previewMode) {
                onView("tasks");
                return;
              }
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
              <span data-testid="cc-lead-elapsed">elapsed {leadElapsed}</span>
              <span>{runningTasks} running · engine {boardHealth.version ?? "—"}</span>
            </div>
            {showIdleTip && (
              <div className="cc-idle-tip" data-testid="cc-idle-tip">{COMMAND_CENTER_IDLE_TIP}</div>
            )}
          </button>

          <div
            className={`cc-flow-connector${flow.afterLead ? " cc-flow-active" : activeStage === "idle" ? " cc-flow-idle" : ""}`}
            data-flow-from="lead"
            aria-hidden="true"
          >
            <i /><span /><em /><b>▼</b>
          </div>

          <button
            className={`cc-card dispatch${activeStage === "dispatch" ? " cc-stage-active" : ""}`}
            data-stage="dispatch"
            onClick={() => onView("workers")}
          >
            <div className="cc-card-topline">
              <strong>dispatch · fork layer</strong>
              <span className="cc-card-count">queue {pending}</span>
            </div>
            <div className="cc-dispatch-bars">
              <div className="cc-dispatch-row"><span>worker load</span><MiniBar value={averageLoad} tone="orange" /><b>{percent(averageLoad)}%</b></div>
              <div className="cc-dispatch-row"><span>pending queue</span><MiniBar value={queuePercent} tone="green" /><b>{pending}</b></div>
              <div className="cc-dispatch-row"><span>online ratio</span><MiniBar value={onlineRatio} tone="blue" /><b>{onlineWorkers}/{workerTotal}</b></div>
            </div>
          </button>

          <div
            className={`cc-flow-connector${flow.afterDispatch ? " cc-flow-active" : activeStage === "idle" ? " cc-flow-idle" : ""}`}
            data-flow-from="dispatch"
            aria-hidden="true"
          >
            <i /><span /><em /><b>▼</b>
          </div>

          <div className="cc-swarm-head">delegate to workers · swarm</div>
          <div className={`cc-swarm-row${activeStage === "swarm" ? " cc-stage-active" : ""}`} data-stage="swarm">
            {swarm.length === 0 ? (
              <div className="cc-swarm-empty">no running subtasks or workers</div>
            ) : swarm.map((card, index) => (
              <button
                key={card.id}
                className={`cc-card swarm cc-swarm-enter${activeStage === "swarm" && card.running ? " cc-stage-active" : ""}`}
                style={{ animationDelay: `${index * 45}ms` }}
                onClick={() => onView(previewMode || lead?.subtasks?.length ? "tasks" : "workers")}
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

          <div
            className={`cc-flow-connector${flow.afterSwarm ? " cc-flow-active" : activeStage === "idle" ? " cc-flow-idle" : ""}`}
            data-flow-from="swarm"
            aria-hidden="true"
          >
            <i /><span /><em /><b>▼</b>
          </div>

          <button
            className={`cc-card review${activeStage === "review" ? " cc-stage-active" : ""}`}
            data-stage="review"
            onClick={() => {
              if (previewMode) {
                onView("tasks");
                return;
              }
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
          <div className="terminal-empty-line" data-testid={showIdleTip ? "cc-idle-tip-log" : undefined}>
            {showIdleTip ? COMMAND_CENTER_IDLE_TIP : "waiting for live events…"}
          </div>
        ) : sessionEvents.map((event, index) => {
          const rowKey = sessionKeys[index] ?? sessionLogEventKey(event);
          return (
            <button
              key={rowKey}
              className={`cc-session-row${enteringSessionKeys.has(rowKey) ? " cc-session-row-enter" : ""}`}
              onClick={() => {
                if (previewMode) {
                  onView("logs");
                  return;
                }
                const taskId = typeof event.details?.task_id === "string" ? event.details.task_id : null;
                if (taskId) {
                  onSelectTask(taskId);
                  onView("tasks");
                } else {
                  onView("logs");
                }
              }}
            >
              <span className="cc-session-line">
                <time>{formatSessionStamp(event.timestamp)}</time>
                {" "}
                <span className="cc-session-msg">{sessionLogLine(event)}</span>
              </span>
            </button>
          );
        })}
      </section>

      <p className="terminal-help-line">提示：顶栏 <kbd>1</kbd>–<kbd>7</kbd> 切视图 · <kbd>Ctrl</kbd><kbd>K</kbd> 聚焦命令 · <kbd>/</kbd> 命令行</p>
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
  const pillValues: StatusPillValues = {
    workers: `${onlineWorkers}/${workers.total}`,
    tasks: String(runningTasks),
    queue: String(tasks.pending_task_count ?? 0),
    grpc: grpcState,
    dash: dashGrpcState,
    events: String(eventLog.length),
    backend: connected ? "online" : "offline",
  };
  const prevPillRef = useRef<StatusPillValues | null>(null);
  const [flashing, setFlashing] = useState<Set<keyof StatusPillValues>>(() => new Set());
  const pillWorkers = pillValues.workers;
  const pillTasks = pillValues.tasks;
  const pillQueue = pillValues.queue;
  const pillGrpc = pillValues.grpc;
  const pillDash = pillValues.dash;
  const pillEvents = pillValues.events;
  const pillBackend = pillValues.backend;

  useEffect(() => {
    const next: StatusPillValues = {
      workers: pillWorkers,
      tasks: pillTasks,
      queue: pillQueue,
      grpc: pillGrpc,
      dash: pillDash,
      events: pillEvents,
      backend: pillBackend,
    };
    const changed = changedPillKeys(prevPillRef.current, next);
    prevPillRef.current = next;
    if (changed.length === 0) return;
    // Timer-deferred setState avoids react-hooks/set-state-in-effect.
    const startTimer = window.setTimeout(() => setFlashing(new Set(changed)), 0);
    const clearTimer = window.setTimeout(() => setFlashing(new Set()), 600);
    return () => {
      window.clearTimeout(startTimer);
      window.clearTimeout(clearTimer);
    };
  }, [pillWorkers, pillTasks, pillQueue, pillGrpc, pillDash, pillEvents, pillBackend]);

  const flashClass = (key: keyof StatusPillValues) => (flashing.has(key) ? " cc-pill-flash" : "");

  return (
    <div className="cc-status-pills" role="region" aria-label="Live status" tabIndex={0}>
      <span className={`cc-pill swarm ${onlineWorkers > 0 ? "hot" : ""}${flashClass("workers")}`}>workers [{pillValues.workers}]</span>
      <span className={`cc-pill lead ${runningTasks > 0 ? "hot" : ""}${flashClass("tasks")}`}>tasks [{pillValues.tasks} running]</span>
      <span className={`cc-pill dispatch ${(tasks.pending_task_count ?? 0) > 0 ? "hot" : ""}${flashClass("queue")}`}>queue [{pillValues.queue}]</span>
      <span className={`cc-pill ${grpcState === "connected" ? "hot lead" : ""}${flashClass("grpc")}`}>grpc [{pillValues.grpc}]</span>
      <span className={`cc-pill ${dashGrpcState === "connected" ? "hot advisor" : "advisor"}${flashClass("dash")}`}>dash [{pillValues.dash}]</span>
      <span className={`cc-pill advisor${flashClass("events")}`}>events [{pillValues.events}]</span>
      <span className={`cc-pill ${connected ? "hot dispatch" : ""}${flashClass("backend")}`}>{connected ? "backend [online]" : "backend [offline]"}</span>
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
  onSetTheme,
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
  initialView = "overview",
}: TerminalDashboardProps) {
  const [view, setView] = useState<TerminalView>(initialView);
  const [command, setCommand] = useState("");
  const [lastCommand, setLastCommand] = useState("—");
  const [notice, setNotice] = useState({ text: "Dashboard 就绪 — 正在读取真实 Docker Gateway", tone: "info" });
  const [clock, setClock] = useState(() => new Date());
  const [busy, setBusy] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [fileBrowserNav, setFileBrowserNav] = useState<FileBrowserNavigateEvent | null>(null);
  const [fileBrowserOpen, setFileBrowserOpen] = useState(false);
  const helpOpenerRef = useRef<HTMLElement | null>(null);
  const fileBrowserOpenerRef = useRef<HTMLElement | null>(null);
  const [fileBrowserLabel, setFileBrowserLabel] = useState<string | null>(null);
  const commandRef = useRef<HTMLInputElement>(null);
  const historyRef = useRef<HTMLElement>(null);
  const appliedDefaultDarkRef = useRef(false);
  const openFileBrowser = useCallback((nav: FileBrowserNavigateEvent) => {
    fileBrowserOpenerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setFileBrowserNav(nav);
    setFileBrowserLabel(`${nav.repoId} · ${nav.path}${nav.line ? `:${nav.line}` : ""}`);
    setFileBrowserOpen(true);
  }, []);
  const closeFileBrowser = useCallback(() => {
    setFileBrowserOpen(false);
    setFileBrowserNav(null);
    const opener = fileBrowserOpenerRef.current;
    if (opener?.isConnected) opener.focus();
  }, []);
  const closeHelp = useCallback(() => {
    setHelpOpen(false);
    const opener = helpOpenerRef.current;
    if (opener?.isConnected) opener.focus();
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  // Deep-linkable views: keep the URL hash in sync with the active tab and
  // follow back/forward navigation. Hash-only history moves fire `hashchange`.
  useEffect(() => {
    const next = hashForView(view);
    if (window.location.hash !== next) window.history.pushState(null, "", next);
  }, [view]);

  useEffect(() => {
    const onHashChange = () => {
      const parsed = viewFromHash(window.location.hash);
      if (parsed) setView(parsed);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  // Prefer dark CC chrome on first visit only (no uc_dashboard_theme key yet).
  // Explicit light/dark in localStorage is always respected — never force-toggle.
  useEffect(() => {
    if (appliedDefaultDarkRef.current) return;
    if (!shouldApplyCommandCenterDefaultDark(theme)) return;
    appliedDefaultDarkRef.current = true;
    if (onSetTheme) {
      onSetTheme("dark");
      return;
    }
    // Fallback when only toggle is wired: flip light → dark once.
    onToggleTheme();
  }, [theme, onSetTheme, onToggleTheme]);

  // Overview autofocus: when selection is null and an active task exists, select it once
  // so swarm cards + session log light up. Never overrides an explicit user selection.
  useEffect(() => {
    if (view !== "overview") return;
    const autofocusId = pickAutofocusTaskId(tasks, selectedTaskId);
    if (autofocusId) onSelectTask(autofocusId);
  }, [view, tasks, selectedTaskId, onSelectTask]);

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
    const isTypingTarget = () => {
      const tag = document.activeElement?.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
        return;
      }
      if (event.key === "Escape" && fileBrowserOpen) {
        event.preventDefault();
        closeFileBrowser();
        return;
      }
      if (event.key === "Escape" && helpOpen) {
        event.preventDefault();
        closeHelp();
        return;
      }
      if (isTypingTarget()) return;
      if (event.key === "?") {
        event.preventDefault();
        if (helpOpen) {
          closeHelp();
        } else {
          helpOpenerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
          setHelpOpen(true);
        }
        return;
      }
      if (helpOpen || paletteOpen) return;
      if (event.key === "/") {
        event.preventDefault();
        commandRef.current?.focus();
        return;
      }
      const letterView = LETTER_VIEWS[event.key.toLowerCase()];
      if (letterView) {
        event.preventDefault();
        setView(letterView);
        return;
      }
      const numeric = Number(event.key);
      const next = VIEWS.find((item) => item.key === String(numeric));
      if (next) setView(next.id);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [helpOpen, paletteOpen, fileBrowserOpen, closeFileBrowser, closeHelp]);

  const currentView = useMemo(() => VIEWS.find((item) => item.id === view) ?? VIEWS[0]!, [view]);
  const runQuickCommand = (value: string) => { void runCommand(value); };

  // Ctrl+K palette: every view plus the dashboard's quick actions.
  const paletteCommands: PaletteCommand[] = [
    ...VIEWS.map((item) => ({
      id: `view-${item.id}`,
      label: `视图 · ${item.label}`,
      hint: `${item.key} · ${item.meta}`,
      keywords: `${item.id} ${item.label} ${item.meta}`,
      run: () => setView(item.id),
    })),    // Recent tasks: running work first, then the newest finished units, so the
    // palette can jump straight into a task from anywhere in the dashboard.
    ...[...tasks.tasks]
      .sort((a, b) => Number(isLiveTask(b)) - Number(isLiveTask(a)))
      .slice(0, 4)
      .map((task) => ({
        id: `task-${task.id}`,
        label: `任务 · ${task.description.slice(0, 24) || "未命名任务"}`,
        hint: `${shortId(task.id)} · ${task.status}`,
        keywords: `${task.id} ${task.description} 任务 task`,
        run: () => {
          onSelectTask(task.id);
          setView("tasks");
        },
      })),
    { id: "cmd-status", label: "运行 · status", hint: "集群摘要", keywords: "status 状态 摘要", run: () => runQuickCommand("status") },
    {
      id: "cmd-submit",
      label: "提交任务 · submit",
      hint: "在命令栏输入 submit",
      keywords: "submit 提交 任务 新建",
      run: () => {
        setView("tasks");
        setCommand("submit ");
        window.setTimeout(() => commandRef.current?.focus(), 0);
      },
    },
    {
      id: "cmd-pause",
      label: "暂停任务 · pause",
      hint: selectedTaskForCommand ? shortId(selectedTaskForCommand) : "未选择任务",
      keywords: "pause 暂停",
      run: () => runQuickCommand(`pause ${selectedTaskForCommand ?? ""}`),
    },
    {
      id: "cmd-resume",
      label: "恢复任务 · resume",
      hint: selectedTaskForCommand ? shortId(selectedTaskForCommand) : "未选择任务",
      keywords: "resume 恢复 继续",
      run: () => runQuickCommand(`resume ${selectedTaskForCommand ?? ""}`),
    },
    { id: "cmd-search", label: "检索代码 · search", hint: "进入 Search", keywords: "search 检索 搜索 代码", run: () => setView("search") },
    { id: "cmd-logs", label: "事件日志 · logs", hint: "进入 Logs", keywords: "logs 日志 事件", run: () => setView("logs") },
    { id: "cmd-refresh", label: "刷新数据 · refresh", hint: "重新拉取面板数据", keywords: "refresh 刷新 重载", run: () => { void onRefresh(); } },
    {
      id: "cmd-theme",
      label: "切换主题 · theme",
      hint: theme === "dark" ? "切到亮色" : "切到暗色",
      keywords: "theme 主题 暗色 亮色 dark light",
      run: () => onToggleTheme(),
    },
    { id: "cmd-help", label: "帮助 · help", hint: "命令速查", keywords: "help 帮助 命令", run: () => runQuickCommand("help") },
  ];

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

      <div className="terminal-mobile-actions" role="region" aria-label="快捷操作">
        <button onClick={() => runQuickCommand("status")}>❯ status</button>
        <button onClick={() => { setView("tasks"); setCommand("submit "); commandRef.current?.focus(); }}>❯ submit</button>
        <button onClick={() => runQuickCommand(`pause ${selectedTaskForCommand ?? ""}`)} disabled={busy}>❯ pause</button>
        <button onClick={() => runQuickCommand(`resume ${selectedTaskForCommand ?? ""}`)} disabled={busy}>❯ resume</button>
        <button onClick={() => setNotice({ text: "事件流由后端推送，不生成假事件", tone: "info" })}>❯ freeze</button>
        <button onClick={() => setView("search")}>⌕ search</button>
        <button onClick={() => runQuickCommand("help")}>? help</button>
      </div>

      <div className="terminal-workspace">
        {view !== "overview" && (
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
        )}

        <main className="terminal-main-panel" aria-label="Dashboard content">
          <div className="terminal-panel-head"><div><h1>{currentView.label}</h1><span>uc / {currentView.meta.toLowerCase()}</span></div><div className={`terminal-panel-meta ${connected ? "online" : "offline"}`}><i />{health.version ?? "engine"} · {connected ? "connected" : "waiting"}</div></div>
          <div className="terminal-view-body">
            {view === "overview" && <OverviewView health={health} workers={workers} tasks={tasks} eventLog={eventLog} metrics={metrics} connected={connected} selectedTask={selectedTask} interactionLog={interactionLog} onView={setView} onSelectTask={onSelectTask} />}
            {view === "tasks" && <div className="terminal-data-view terminal-task-layout">
              <TasksPanel data={tasks} interactionLog={interactionLog} onFlush={onFlush} onPauseTask={onPauseTask} onResumeTask={onResumeTask} onCancelTask={onCancelTask} stale={grpcStale} grpcSubmitTask={grpcSubmitTask} onTaskCreated={onTaskCreated} onOptimisticAdd={onOptimisticAdd} onSelectTask={onSelectTask} selectedTaskId={selectedTaskId} showInlineDetail={false} onNavigateFile={openFileBrowser} />
              <section className="terminal-task-history" ref={historyRef} aria-label="Selected task history">
                <div className="terminal-task-history-head">
                  <span>任务状态与历史日志</span>
                  {selectedTask && <span className={statusBadgeClass(selectedTask.status)}>{selectedTask.status}</span>}
                </div>
                {selectedTask ? <>
                  <TaskHistoryLiveStrip task={selectedTask} />
                  <strong className="terminal-task-history-title">{selectedTask.description}</strong>
                  <small className="terminal-task-history-meta">{selectedTask.id} · {taskHistoryLoading ? "正在读取历史日志…" : taskHistoryError ? `历史日志读取失败：${taskHistoryError}` : `${interactionLog[selectedTask.id]?.length ?? 0} 条日志`}</small>
                  <TaskDetail key={selectedTask.id} task={selectedTask} interactionLog={interactionLog[selectedTask.id] ?? []} onNavigateFile={openFileBrowser} variant="terminal" />
                </> : <p className="terminal-empty-line">当前没有可查看的历史任务</p>}
              </section>
            </div>}
            {view === "workers" && (
              <div className="terminal-data-view terminal-workers-layout">
                <WorkersPanel
                  workers={workers}
                  tasks={tasks}
                  stale={grpcStale}
                  variant="terminal"
                  onJumpTask={(id) => { onSelectTask(id); setView("tasks"); }}
                />
              </div>
            )}
            {view === "inference" && (
              <div className="terminal-data-view terminal-inference-layout">
                <InferencePanel variant="terminal" />
              </div>
            )}
            {view === "logs" && (
              <div className="terminal-data-view terminal-logs-layout">
                <EventLogPanel
                  events={eventLog}
                  stale={grpcStale}
                  variant="terminal"
                  onSelectTask={(id) => { onSelectTask(id); setView("tasks"); }}
                />
              </div>
            )}
            {view === "search" && (
              <div className="terminal-data-view terminal-search-layout">
                <SearchPanel
                  grpcState={grpcState}
                  onNavigateFile={openFileBrowser}
                  stale={grpcStale}
                  variant="terminal"
                />
              </div>
            )}
            {view === "memory" && <div className="terminal-empty-contract"><span className="terminal-empty-icon">◌</span><h2>Memory surface</h2><p>记忆检索仍由 EngineService 暴露；当前 dashboard Connect 合约未提供独立 Memory RPC。使用 Search 进入真实检索入口。</p><button onClick={() => setView("search")}>打开 Search</button></div>}
          </div>
          <div key={notice.text} className={`terminal-notice ${notice.tone}`}><span>❯</span>{notice.text}</div>
        </main>

        {view !== "overview" && <AdvisorRail connected={connected} workers={workers} tasks={tasks} eventLog={eventLog} health={health} metrics={metrics} onView={setView} />}
      </div>

      <StatusPillBar connected={connected} grpcState={grpcState} dashGrpcState={dashGrpcState} workers={workers} tasks={tasks} eventLog={eventLog} />
      <footer className="terminal-command-bar"><span>uc ❯</span><input ref={commandRef} value={command} onChange={(event) => setCommand(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void runCommand(); } }} placeholder='submit "fix flaky heartbeat test"' aria-label="uc 命令行" /><button className={busy ? "is-busy" : undefined} onClick={() => void runCommand()} disabled={!command.trim() || busy}>{busy ? "运行中" : "运行"}</button><kbd>↵</kbd><small>运行 · / 聚焦</small><span className="terminal-footer-status">{connected ? "后端在线" : "后端离线"} · engine {health.version ?? "—"} · {lastUpdate ? new Date(lastUpdate).toLocaleTimeString() : "waiting"}</span></footer>
      <div className="terminal-keybar" role="region" aria-label="Keyboard shortcuts"><span><kbd>Tab</kbd> 面板</span><span><kbd>1</kbd>–<kbd>7</kbd> 视图</span><span><kbd>↑↓</kbd> 主操作</span><span><kbd>⏎</kbd> 执行</span><span><kbd>/</kbd> 命令行</span><span><kbd>?</kbd> 帮助</span><span className="terminal-keybar-right">{connected ? "在线" : "离线"} · dashboard · MIT</span></div>
      <nav className="terminal-mobile-nav" aria-label="移动端视图导航">{VIEWS.map((item) => <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)}><span>{item.icon}</span>{item.label}</button>)}</nav>
      {fileBrowserOpen && (
        <div className="terminal-file-browser-panel" role="dialog" aria-label="File browser">
          <div className="terminal-file-browser-head">
            <span>
              <b>文件浏览器</b>
              {fileBrowserLabel ? <small>{fileBrowserLabel}</small> : <small>选择一个仓库与文件</small>}
            </span>
            <button type="button" onClick={closeFileBrowser} aria-label="关闭文件浏览器">✕</button>
          </div>
          <div className="terminal-file-browser-fallback">
            <Suspense fallback={<div className="terminal-panel-loading" role="status">载入文件浏览器…</div>}>
              <FileBrowser initialNav={fileBrowserNav} onNavConsumed={() => setFileBrowserNav(null)} stale={grpcStale} />
            </Suspense>
          </div>
        </div>
      )}
      <div className="terminal-connection-diagnostics" role="region" aria-label="Connection diagnostics"><ConnectionPill label="Task" state={grpcState} /><ConnectionPill label="Dashboard" state={dashGrpcState} />{grpcExhausted && <button onClick={onReconnectGrpc}>↻ retry gRPC</button>}<button onClick={onReconnectDashGrpc}>↻ retry dashboard</button></div>
      {Object.keys(fetchErrors).length > 0 && <div className="terminal-fetch-warning" role="region" aria-label="Fetch warnings">⚠ 部分面板不可用：{Object.keys(fetchErrors).join(", ")}</div>}
      {helpOpen && (
        <div className="terminal-palette-backdrop" role="presentation" onClick={closeHelp}>
          <div
            className="terminal-palette terminal-help-panel"
            role="dialog"
            aria-modal="true"
            aria-label="Keyboard shortcuts"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="terminal-help-head">
              <strong>键盘快捷键</strong>
              <small>? 或 Esc 关闭</small>
            </div>
            <div className="terminal-help-grid">
              <section>
                <h3>视图</h3>
                <ul>
                  <li><kbd>1</kbd><kbd>…</kbd><kbd>7</kbd><span>按顺序切换视图</span></li>
                  <li><kbd>O</kbd><span>概览</span></li>
                  <li><kbd>T</kbd><span>任务</span></li>
                  <li><kbd>W</kbd><span>工作节点</span></li>
                  <li><kbd>S</kbd><span>检索</span></li>
                  <li><kbd>M</kbd><span>记忆</span></li>
                  <li><kbd>E</kbd><span>日志</span></li>
                  <li><kbd>I</kbd><span>推理实验</span></li>
                </ul>
              </section>
              <section>
                <h3>命令</h3>
                <ul>
                  <li><kbd>Ctrl</kbd><kbd>K</kbd><span>命令面板</span></li>
                  <li><kbd>/</kbd><span>聚焦命令栏</span></li>
                  <li><kbd>↑</kbd><kbd>↓</kbd><span>主操作</span></li>
                  <li><kbd>⏎</kbd><span>执行命令</span></li>
                  <li><kbd>Tab</kbd><span>面板切换</span></li>
                </ul>
              </section>
            </div>
            <div className="terminal-palette-foot">? 帮助 · Esc 关闭 · Ctrl+K 命令面板</div>
          </div>
        </div>
      )}
      <CommandPalette open={paletteOpen} commands={paletteCommands} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
