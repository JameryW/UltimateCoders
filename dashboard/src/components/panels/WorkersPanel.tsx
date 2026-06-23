import { useState, useMemo, memo } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn, shortId } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import type { WorkersData, WorkerInfo, TasksData, SubtaskSummary } from "@/types/dashboard";

function loadBarColor(percent: number): string {
  if (percent >= 100) return "bg-red-500";
  if (percent >= 75) return "bg-yellow-500";
  return "bg-green-500";
}

function formatHeartbeatAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s ago`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m ago`;
}

// ponytail: inline helpers — no new utils file needed
const statusBadgeClass = (status: string) => {
  switch (status) {
    case "completed": return "bg-green-500/20 text-green-400";
    case "in_progress": return "bg-cyan-500/20 text-cyan-400";
    case "assigned": return "bg-blue-500/20 text-blue-400";
    case "failed": return "bg-red-500/20 text-red-400";
    case "conflicted": return "bg-yellow-500/20 text-yellow-400";
    default: return "bg-[var(--bg-surface-alt)] text-[var(--text-secondary)]";
  }
};
const truncate = (s: string, n: number) => s.length > n ? s.slice(0, n) + "…" : s;

function WorkerDetail({ worker, activeSubtasks, onJumpTask }: { worker: WorkerInfo; activeSubtasks: SubtaskSummary[]; onJumpTask?: (taskId: string) => void }) {
  // ponytail: derive task id from subtask id prefix — subtask ids are "taskId-subtaskN"
  const taskIdFromSubtask = (subtaskId: string) => {
    const lastDash = subtaskId.lastIndexOf("-");
    return lastDash > 0 ? subtaskId.substring(0, lastDash) : subtaskId;
  };

  return (
    <div className="mt-2 ml-1 space-y-1.5 text-xs border-t border-[var(--border-color)] pt-2">
      <div className="flex items-start gap-2">
        <span className="text-[var(--text-muted)] shrink-0 w-20">Full ID</span>
        <span className="font-mono text-[var(--text-primary)] break-all">{worker.id}</span>
      </div>
      <div className="flex items-start gap-2">
        <span className="text-[var(--text-muted)] shrink-0 w-20">Heartbeat</span>
        <span className="text-[var(--text-primary)]">
          {new Date(worker.last_heartbeat).toLocaleString()}
        </span>
      </div>
      <div className="flex items-start gap-2">
        <span className="text-[var(--text-muted)] shrink-0 w-20">Age</span>
        <span className={cn(worker.heartbeat_stale ? "text-yellow-400" : "text-[var(--text-primary)]")}>
          {formatHeartbeatAge(worker.heartbeat_age_seconds)}
          {worker.heartbeat_stale && " (stale)"}
        </span>
      </div>
      <div className="flex items-start gap-2">
        <span className="text-[var(--text-muted)] shrink-0 w-20">Load</span>
        <span className="text-[var(--text-primary)]">
          {worker.current_load} / {worker.max_capacity} ({worker.load_percent}%)
        </span>
      </div>
      {worker.capabilities.length > 0 && (
        <div className="flex items-start gap-2">
          <span className="text-[var(--text-muted)] shrink-0 w-20">Capabilities</span>
          <div className="flex flex-wrap gap-1">
            {worker.capabilities.map((cap) => (
              <span
                key={cap}
                className="bg-[var(--bg-surface-alt)] text-[var(--text-secondary)] px-1.5 py-0.5 rounded"
              >
                {cap}
              </span>
            ))}
          </div>
        </div>
      )}
      {activeSubtasks.length > 0 && (
        <div className="flex items-start gap-2">
          <span className="text-[var(--text-muted)] shrink-0 w-20">Subtasks</span>
          <div className="space-y-0.5">
            {activeSubtasks.map((st) => (
              <div
                key={st.id}
                className={cn(
                  "flex items-center gap-1",
                  onJumpTask && "cursor-pointer hover:bg-[var(--bg-surface-alt)]/50 rounded px-1 -mx-1"
                )}
                role={onJumpTask ? "button" : undefined}
                tabIndex={onJumpTask ? 0 : undefined}
                onClick={onJumpTask ? () => onJumpTask(taskIdFromSubtask(st.id)) : undefined}
                onKeyDown={onJumpTask ? (e) => { if (e.key === "Enter") onJumpTask(taskIdFromSubtask(st.id)); } : undefined}
              >
                <span className={cn("px-1 rounded text-[10px]", statusBadgeClass(st.status))}>{st.status}</span>
                <span className="text-[var(--text-primary)]">{shortId(st.id)} {truncate(st.description, 30)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export const WorkersPanel = memo(function WorkersPanel({
  workers,
  tasks,
  stale,
  onJumpTask,
  embedded,
}: {
  workers: WorkersData;
  tasks: TasksData;
  stale?: boolean;
  onJumpTask?: (taskId: string) => void;
  /** When true, render without Card wrapper (embedded in SidebarPanel) */
  embedded?: boolean;
}) {
  const [expandedWorkerId, setExpandedWorkerId] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<"load" | "name">("load");
  const [filterOnline, setFilterOnline] = useState<boolean | null>(null);

  // ponytail: Map<workerId, SubtaskSummary[]> — O(tasks × subtasks), fine for dashboard scale
  const workerSubtasks = useMemo(() => {
    const map = new Map<string, SubtaskSummary[]>();
    for (const t of tasks.tasks) {
      for (const s of t.subtasks ?? []) {
        if (s.assigned_worker) {
          const arr = map.get(s.assigned_worker) ?? [];
          arr.push(s);
          map.set(s.assigned_worker, arr);
        }
      }
    }
    return map;
  }, [tasks]);

  // ponytail: sort + filter workers
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

  const content = (
    <>
      {!workers.available ? (
        <p className="text-sm text-[var(--text-muted)]"><Badge variant="unavailable">Unavailable</Badge></p>
      ) : workers.workers.length === 0 ? (
        <EmptyState icon="workers" title="No workers connected" description="Workers will appear here when they connect to the engine" />
      ) : (
        <>
          <div className="flex items-center gap-2 mb-2">
            <button
              onClick={() => setSortBy(sortBy === "load" ? "name" : "load")}
              className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer"
            >
              Sort: {sortBy === "load" ? "Load" : "Name"}
            </button>
            <button
              onClick={() => setFilterOnline(filterOnline === true ? null : true)}
              className={"text-xs cursor-pointer " + (filterOnline === true ? "text-green-400" : "text-[var(--text-muted)] hover:text-[var(--text-primary)]")}
            >
              Online
            </button>
            <button
              onClick={() => setFilterOnline(filterOnline === false ? null : false)}
              className={"text-xs cursor-pointer " + (filterOnline === false ? "text-red-400" : "text-[var(--text-muted)] hover:text-[var(--text-primary)]")}
            >
              Offline
            </button>
          </div>
          <ul className="space-y-2">
          {sortedWorkers.map((w) => {
            const activeSubtasks = workerSubtasks.get(w.id) ?? [];
            const activeCount = activeSubtasks.filter(
              (s) => s.status === "in_progress" || s.status === "assigned"
            ).length;
            // ponytail: subtask progress bar — completed/total from all assigned subtasks
            const subtaskCompleted = activeSubtasks.filter((s) => s.status === "completed").length;
            const subtaskTotal = activeSubtasks.length;
            const subtaskPercent = subtaskTotal > 0 ? Math.round((subtaskCompleted / subtaskTotal) * 100) : 0;
            return (
              <li
                key={w.id}
                className={cn(
                  "border-l-2 pl-2 py-1",
                  w.is_available ? "border-l-green-500" : "border-l-red-500"
                )}
              >
                <div
                  role="button"
                  tabIndex={0}
                  aria-expanded={expandedWorkerId === w.id}
                  aria-label={`Worker ${shortId(w.id)}`}
                  className="cursor-pointer hover:bg-[var(--bg-surface-alt)]/50 rounded-r"
                  onClick={() => toggleExpand(w.id)}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleExpand(w.id); } }}
                >
                  <div className="flex items-center justify-between text-sm">
                    <span className="font-mono text-[var(--text-primary)]">{shortId(w.id)}</span>
                    <div className="flex items-center gap-1.5">
                      {activeCount > 0 && (
                        <span className="text-xs bg-cyan-500/20 text-cyan-400 px-1.5 py-0.5 rounded" title={`${activeCount} active subtask(s)`}>
                          {activeCount} active
                        </span>
                      )}
                      {/* ponytail: subtask progress micro-bar */}
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
                        <span className="text-yellow-500 text-xs" title="Heartbeat stale">
                          &#9888;
                        </span>
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
                      <span>
                        {w.current_load}/{w.max_capacity}
                      </span>
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

                {/* Detail expansion */}
                {expandedWorkerId === w.id && (
                  <WorkerDetail worker={w} activeSubtasks={activeSubtasks} onJumpTask={onJumpTask} />
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
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Workers</CardTitle>
        <Badge variant="ok">
          {workers.available_count}/{workers.total}
        </Badge>
      </CardHeader>
      {content}
    </Card>
  );
});
