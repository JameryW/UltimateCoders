import { useState, useMemo, memo } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn, shortId, truncate, statusBadgeClass } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import { clampPercent, isSubtaskDone, isSubtaskRunning } from "@/lib/taskProgress";
import type { WorkersData, WorkerInfo, TasksData, SubtaskSummary } from "@/types/dashboard";

type BarTone = "green" | "blue" | "amber" | "orange" | "purple" | "red";

/** Neon rail state for a worker row under command-center chrome. */
export type WorkerRowNeon = "online" | "loaded" | "offline" | "stale";

/** Map worker availability / load / heartbeat → CC neon rail class. */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function workerRowNeon(worker: Pick<WorkerInfo, "is_available" | "load_percent" | "heartbeat_stale">): WorkerRowNeon {
  if (!worker.is_available) return "offline";
  if (worker.heartbeat_stale) return "stale";
  if (clampPercent(worker.load_percent) > 0) return "loaded";
  return "online";
}

/** Load meter tone from percent thresholds (CC tokens). */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function workerLoadTone(percent: number): BarTone {
  const pct = clampPercent(percent);
  if (pct >= 100) return "red";
  if (pct >= 75) return "orange";
  if (pct >= 40) return "blue";
  return "green";
}

function MiniBar({
  value,
  tone = "blue",
  testId,
}: {
  value: number;
  tone?: BarTone;
  testId?: string;
}) {
  return (
    <span className={`terminal-mini-bar ${tone}`} data-testid={testId} aria-hidden="true">
      <i style={{ width: `${clampPercent(value)}%` }} />
    </span>
  );
}

function formatHeartbeatAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s ago`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m ago`;
}

/** Best-effort hostname from the worker's opaque registration metadata. */
function workerHostname(metadata?: string): string | null {
  if (!metadata) return null;
  try {
    const host = (JSON.parse(metadata) as { hostname?: unknown }).hostname;
    return typeof host === "string" && host.length > 0 ? host : null;
  } catch {
    return null;
  }
}

function AssignmentSwarmRow({
  subtask,
  parentTaskId,
  onJumpTask,
}: {
  subtask: SubtaskSummary;
  parentTaskId: string;
  onJumpTask?: (taskId: string) => void;
}) {
  const done = isSubtaskDone(subtask);
  const running = isSubtaskRunning(subtask);
  const pct = clampPercent(subtask.percent ?? (done ? 100 : 0));
  const marker = done ? "✓ done" : running ? "◐ running" : "◇ idle";
  const interactive = Boolean(onJumpTask);

  return (
    <div
      className={cn("terminal-swarm-row", done && "is-done", running && "is-running")}
      data-testid="terminal-swarm-row"
      data-subtask-id={subtask.id}
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={interactive ? () => onJumpTask?.(parentTaskId) : undefined}
      onKeyDown={interactive ? (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          onJumpTask?.(parentTaskId);
        }
      } : undefined}
    >
      <div className="terminal-swarm-row-topline">
        <strong title={subtask.id}>{shortId(subtask.id, 12)}</strong>
        <span className={cn("terminal-swarm-marker", done ? "done" : running ? "running" : "idle")}>
          {marker}
        </span>
        <b className="terminal-swarm-pct">{pct}%</b>
      </div>
      <MiniBar value={pct} tone="blue" />
      <div className="terminal-swarm-row-meta">
        <span className="terminal-swarm-desc">{truncate(subtask.description, 48)}</span>
        {subtask.phase && <span className="terminal-swarm-phase">{subtask.phase}</span>}
        {subtask.step_agent && <span className="terminal-swarm-agent">{subtask.step_agent}</span>}
        <span className={cn("text-xs px-1 py-0 rounded", statusBadgeClass(subtask.status))}>{subtask.status}</span>
      </div>
    </div>
  );
}

function WorkerDetail({
  worker,
  activeSubtasks,
  subtaskToTask,
  onJumpTask,
  terminal,
}: {
  worker: WorkerInfo;
  activeSubtasks: SubtaskSummary[];
  subtaskToTask: Map<string, string>;
  onJumpTask?: (taskId: string) => void;
  terminal: boolean;
}) {
  const taskIdFromSubtask = (subtaskId: string) => subtaskToTask.get(subtaskId) ?? subtaskId;
  const hostname = workerHostname(worker.metadata);

  return (
    <div className={cn(
      "mt-2 ml-1 space-y-1.5 text-xs border-t pt-2",
      terminal ? "terminal-worker-detail" : "border-[var(--border-color)]",
    )}>
      <div className="flex items-start gap-2">
        <span className="text-[var(--text-muted)] shrink-0 w-20">Full ID</span>
        <span className="font-mono text-[var(--text-primary)] break-all">{worker.id}</span>
      </div>
      {hostname && (
        <div className="flex items-start gap-2">
          <span className="text-[var(--text-muted)] shrink-0 w-20">Host</span>
          <span className="font-mono text-[var(--text-primary)]">{hostname}</span>
        </div>
      )}
      <div className="flex items-start gap-2">
        <span className="text-[var(--text-muted)] shrink-0 w-20">Heartbeat</span>
        <span className="text-[var(--text-primary)]">
          {worker.last_heartbeat ? new Date(worker.last_heartbeat).toLocaleString() : "—"}
        </span>
      </div>
      <div className="flex items-start gap-2">
        <span className="text-[var(--text-muted)] shrink-0 w-20">Age</span>
        <span className={cn(
          worker.heartbeat_stale
            ? (terminal ? "text-[var(--cc-lead)]" : "text-yellow-400")
            : "text-[var(--text-primary)]",
        )}>
          {formatHeartbeatAge(worker.heartbeat_age_seconds)}
          {worker.heartbeat_stale && " (stale)"}
        </span>
      </div>
      <div className="flex items-start gap-2">
        <span className="text-[var(--text-muted)] shrink-0 w-20">Load</span>
        <span className="font-mono text-[var(--text-primary)]">
          {worker.current_load} / {worker.max_capacity} ({clampPercent(worker.load_percent)}%)
        </span>
      </div>
      {worker.capabilities.length > 0 && (
        <div className="flex items-start gap-2">
          <span className="text-[var(--text-muted)] shrink-0 w-20">Capabilities</span>
          <div className="flex flex-wrap gap-1">
            {worker.capabilities.map((cap) => (
              <span
                key={cap}
                className={terminal ? "terminal-worker-cap" : "bg-[var(--bg-surface-alt)] text-[var(--text-secondary)] px-1.5 py-0.5 rounded"}
              >
                {cap}
              </span>
            ))}
          </div>
        </div>
      )}
      {!terminal && activeSubtasks.length > 0 && (
        <div className="flex items-start gap-2">
          <span className="text-[var(--text-muted)] shrink-0 w-20">Subtasks</span>
          <div className="space-y-0.5">
            {activeSubtasks.map((st) => (
              <div
                key={st.id}
                className={cn(
                  "flex items-center gap-1",
                  onJumpTask && "cursor-pointer hover:bg-[var(--bg-surface-alt)]/50 rounded px-1 -mx-1",
                )}
                role={onJumpTask ? "button" : undefined}
                tabIndex={onJumpTask ? 0 : undefined}
                onClick={onJumpTask ? () => onJumpTask(taskIdFromSubtask(st.id)) : undefined}
                onKeyDown={onJumpTask ? (e) => { if (e.key === "Enter") onJumpTask(taskIdFromSubtask(st.id)); } : undefined}
              >
                <span className={cn("px-1 rounded text-[10px]", statusBadgeClass(st.status))}>{st.status}</span>
                <span className="text-[var(--text-primary)]">{shortId(st.id)} {truncate(st.description, 30)}</span>
                {st.step_agent && (
                  <span className="text-[10px] bg-cyan-500/20 text-cyan-400 px-1.5 py-0.5 rounded font-mono shrink-0" title={`Coding agent: ${st.step_agent}`}>
                    {st.step_agent}
                  </span>
                )}
                {st.phase && (
                  <span className="text-[10px] text-blue-400 truncate shrink-0" title={st.phase}>{st.phase}</span>
                )}
                {st.percent != null && (
                  <span className="text-[10px] text-[var(--text-muted)] tabular-nums shrink-0">{Math.round(st.percent)}%</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function loadBarColor(percent: number): string {
  if (percent >= 100) return "bg-red-500";
  if (percent >= 75) return "bg-yellow-500";
  return "bg-green-500";
}

export const WorkersPanel = memo(function WorkersPanel({
  workers,
  tasks,
  stale,
  onJumpTask,
  embedded,
  variant = "default",
}: {
  workers: WorkersData;
  tasks: TasksData;
  stale?: boolean;
  onJumpTask?: (taskId: string) => void;
  /** When true, render without Card wrapper (embedded in SidebarPanel) */
  embedded?: boolean;
  /** Command-center: neon rails, mini-bar load, swarm assignment strip. */
  variant?: "default" | "terminal";
}) {
  const terminal = variant === "terminal";
  const [expandedWorkerId, setExpandedWorkerId] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<"load" | "name">("load");
  const [filterOnline, setFilterOnline] = useState<boolean | null>(null);

  const { workerSubtasks, subtaskToTask } = useMemo(() => {
    const wMap = new Map<string, SubtaskSummary[]>();
    const sMap = new Map<string, string>();
    for (const t of tasks.tasks) {
      for (const s of t.subtasks ?? []) {
        sMap.set(s.id, t.id);
        if (s.assigned_worker) {
          const arr = wMap.get(s.assigned_worker) ?? [];
          arr.push(s);
          wMap.set(s.assigned_worker, arr);
        }
      }
    }
    return { workerSubtasks: wMap, subtaskToTask: sMap };
  }, [tasks]);

  const sortedWorkers = useMemo(() => {
    let list = [...workers.workers];
    if (filterOnline === true) list = list.filter((w) => w.is_available);
    if (filterOnline === false) list = list.filter((w) => !w.is_available);
    if (sortBy === "load") list.sort((a, b) => b.load_percent - a.load_percent);
    else list.sort((a, b) => a.id.localeCompare(b.id));
    return list;
  }, [workers.workers, sortBy, filterOnline]);

  const toggleExpand = (workerId: string) => {
    setExpandedWorkerId(expandedWorkerId === workerId ? null : workerId);
  };

  const clusterSummary = useMemo(() => {
    if (!workers.available || workers.workers.length === 0) return null;
    const totalLoad = workers.workers.reduce((s, w) => s + w.current_load, 0);
    const totalCap = workers.workers.reduce((s, w) => s + w.max_capacity, 0);
    const pct = totalCap > 0 ? Math.round((totalLoad / totalCap) * 100) : 0;
    return { totalLoad, totalCap, pct };
  }, [workers]);

  const content = (
    <>
      {!workers.available ? (
        <p className="text-sm text-[var(--text-muted)]"><Badge variant="unavailable">Unavailable</Badge></p>
      ) : workers.workers.length === 0 ? (
        <EmptyState icon="workers" title="No workers connected" description="Workers will appear here when they connect to the engine" />
      ) : (
        <>
          <div className={cn("flex items-center gap-2 mb-2", terminal && "terminal-worker-filters")}>
            <button
              onClick={() => setSortBy(sortBy === "load" ? "name" : "load")}
              className={cn(
                "text-xs cursor-pointer",
                terminal ? "terminal-worker-filter-btn" : "text-[var(--text-muted)] hover:text-[var(--text-primary)]",
              )}
            >
              Sort: {sortBy === "load" ? "Load" : "Name"}
            </button>
            <button
              onClick={() => setFilterOnline(filterOnline === true ? null : true)}
              className={cn(
                "text-xs cursor-pointer",
                terminal
                  ? cn("terminal-worker-filter-btn", filterOnline === true && "is-active swarm")
                  : (filterOnline === true ? "text-green-400" : "text-[var(--text-muted)] hover:text-[var(--text-primary)]"),
              )}
            >
              Online
            </button>
            <button
              onClick={() => setFilterOnline(filterOnline === false ? null : false)}
              className={cn(
                "text-xs cursor-pointer",
                terminal
                  ? cn("terminal-worker-filter-btn", filterOnline === false && "is-active offline")
                  : (filterOnline === false ? "text-red-400" : "text-[var(--text-muted)] hover:text-[var(--text-primary)]"),
              )}
            >
              Offline
            </button>
          </div>
          <ul className={cn(terminal ? "terminal-worker-list" : "space-y-2")} aria-label="Worker list">
            {sortedWorkers.map((w) => {
              const activeSubtasks = workerSubtasks.get(w.id) ?? [];
              const activeCount = activeSubtasks.filter(
                (s) => s.status === "in_progress" || s.status === "assigned" || isSubtaskRunning(s),
              ).length;
              const subtaskCompleted = activeSubtasks.filter((s) => isSubtaskDone(s)).length;
              const subtaskTotal = activeSubtasks.length;
              const subtaskPercent = subtaskTotal > 0 ? Math.round((subtaskCompleted / subtaskTotal) * 100) : 0;
              const neon = workerRowNeon(w);
              const expanded = expandedWorkerId === w.id;
              const hostname = workerHostname(w.metadata);
              const loadPct = clampPercent(w.load_percent);

              if (terminal) {
                return (
                  <li key={w.id} className="terminal-worker-item">
                    <div
                      role="button"
                      tabIndex={0}
                      aria-expanded={expanded}
                      aria-label={`Worker ${shortId(w.id)}`}
                      data-testid="terminal-worker-row"
                      data-neon={neon}
                      className={cn(
                        "terminal-worker-row",
                        `is-${neon}`,
                        expanded && "is-expanded",
                      )}
                      onClick={() => toggleExpand(w.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          toggleExpand(w.id);
                        }
                      }}
                    >
                      <div className="terminal-worker-row-topline">
                        <span className="terminal-worker-id">
                          {shortId(w.id)}
                          {hostname && (
                            <span className="terminal-worker-host" title="Host this worker registered from">
                              @{hostname}
                            </span>
                          )}
                        </span>
                        <div className="terminal-worker-row-badges">
                          {activeCount > 0 && (
                            <span className="terminal-worker-active" title={`${activeCount} active subtask(s)`}>
                              {activeCount} active
                            </span>
                          )}
                          {subtaskTotal > 0 && (
                            <span className="terminal-worker-subtask-count" title={`${subtaskCompleted}/${subtaskTotal} subtasks completed`}>
                              {subtaskCompleted}/{subtaskTotal}
                            </span>
                          )}
                          {w.heartbeat_stale && (
                            <span className="terminal-worker-stale" title="Heartbeat stale">stale</span>
                          )}
                          <span className={cn("terminal-worker-avail", w.is_available ? "online" : "offline")}>
                            {w.is_available ? "online" : "offline"}
                          </span>
                        </div>
                      </div>

                      <div className="terminal-worker-load-row">
                        <MiniBar value={loadPct} tone={workerLoadTone(loadPct)} testId="terminal-worker-load" />
                        <span className="terminal-worker-load-meta">
                          <b>{w.current_load}/{w.max_capacity}</b>
                          <em>{loadPct}%</em>
                        </span>
                      </div>

                      {subtaskTotal > 0 && (
                        <div className="terminal-worker-assign-progress" title={`${subtaskCompleted}/${subtaskTotal} assigned complete`}>
                          <MiniBar value={subtaskPercent} tone={subtaskPercent >= 100 ? "green" : "blue"} />
                        </div>
                      )}

                      {w.capabilities.length > 0 && (
                        <div className="terminal-worker-caps">
                          {w.capabilities.map((cap) => (
                            <span key={cap} className="terminal-worker-cap">{cap}</span>
                          ))}
                        </div>
                      )}
                    </div>

                    {activeSubtasks.length > 0 && (
                      <div className="terminal-worker-assignments" data-testid="terminal-worker-assignments">
                        <div className="terminal-worker-assignments-head">assigned · swarm</div>
                        {activeSubtasks.map((st) => (
                          <AssignmentSwarmRow
                            key={st.id}
                            subtask={st}
                            parentTaskId={subtaskToTask.get(st.id) ?? st.id}
                            onJumpTask={onJumpTask}
                          />
                        ))}
                      </div>
                    )}

                    {expanded && (
                      <WorkerDetail
                        worker={w}
                        activeSubtasks={activeSubtasks}
                        subtaskToTask={subtaskToTask}
                        onJumpTask={onJumpTask}
                        terminal
                      />
                    )}
                  </li>
                );
              }

              return (
                <li
                  key={w.id}
                  className={cn(
                    "border-l-2 pl-2 py-1",
                    w.is_available ? "border-l-green-500" : "border-l-red-500",
                  )}
                >
                  <div
                    role="button"
                    tabIndex={0}
                    aria-expanded={expanded}
                    aria-label={`Worker ${shortId(w.id)}`}
                    className="cursor-pointer hover:bg-[var(--bg-surface-alt)]/50 rounded-r"
                    onClick={() => toggleExpand(w.id)}
                    onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleExpand(w.id); } }}
                  >
                    <div className="flex items-center justify-between text-sm">
                      <span className="font-mono text-[var(--text-primary)] flex items-center gap-1.5">
                        {shortId(w.id)}
                        {hostname && (
                          <span
                            className="text-[10px] font-sans text-[var(--text-muted)] bg-[var(--bg-surface-alt)] px-1 py-0.5 rounded"
                            title="Host this worker registered from"
                          >
                            @{hostname}
                          </span>
                        )}
                      </span>
                      <div className="flex items-center gap-1.5">
                        {activeCount > 0 && (
                          <span className="text-xs bg-cyan-500/20 text-cyan-400 px-1.5 py-0.5 rounded" title={`${activeCount} active subtask(s)`}>
                            {activeCount} active
                          </span>
                        )}
                        {subtaskTotal > 0 && (
                          <div className="flex items-center gap-1 text-xs text-[var(--text-muted)]" title={`${subtaskCompleted}/${subtaskTotal} subtasks completed`}>
                            <div className="w-16 h-1.5 bg-[var(--bg-surface-alt)] rounded overflow-hidden">
                              <div
                                className={cn("h-full rounded", subtaskPercent >= 100 ? "bg-green-500" : subtaskPercent >= 50 ? "bg-cyan-500" : "bg-blue-500")}
                                style={{ width: `${subtaskPercent}%` }}
                              />
                            </div>
                            <span>{subtaskCompleted}/{subtaskTotal}</span>
                          </div>
                        )}
                        {w.heartbeat_stale && (
                          <span className="text-yellow-500 text-xs" title="Heartbeat stale">&#9888;</span>
                        )}
                      </div>
                    </div>

                    <div className="mt-1">
                      <div className="load-bar w-full">
                        <div
                          className={cn("load-bar-fill", loadBarColor(w.load_percent))}
                          style={{ width: `${Math.min(w.load_percent, 100)}%` }}
                        />
                      </div>
                      <div className="flex justify-between text-xs text-[var(--text-muted)] mt-0.5">
                        <span>{w.current_load}/{w.max_capacity}</span>
                        <span>{w.load_percent}%</span>
                      </div>
                    </div>

                    {w.capabilities.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1">
                        {w.capabilities.map((cap) => (
                          <span
                            key={cap}
                            className="text-xs bg-[var(--bg-surface-alt)] text-[var(--text-secondary)] px-1.5 py-0.5 rounded"
                          >
                            {cap}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>

                  {expanded && (
                    <WorkerDetail
                      worker={w}
                      activeSubtasks={activeSubtasks}
                      subtaskToTask={subtaskToTask}
                      onJumpTask={onJumpTask}
                      terminal={false}
                    />
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </>
  );

  if (embedded) return content;

  return (
    <Card stale={stale} className={terminal ? "terminal-workers-card" : undefined}>
      <CardHeader>
        <CardTitle>Workers</CardTitle>
        <div className="flex items-center gap-2">
          <Badge variant="ok">
            {workers.available_count}/{workers.total}
          </Badge>
          {clusterSummary && (
            <span className={cn(
              "text-xs font-mono",
              terminal
                ? (clusterSummary.pct >= 80 ? "text-[var(--cc-lead)]" : "text-[var(--terminal-dim)]")
                : (clusterSummary.pct >= 80 ? "text-yellow-400" : "text-[var(--text-muted)]"),
            )}>
              {clusterSummary.totalLoad}/{clusterSummary.totalCap} ({clusterSummary.pct}%)
            </span>
          )}
        </div>
      </CardHeader>
      {content}
    </Card>
  );
});
