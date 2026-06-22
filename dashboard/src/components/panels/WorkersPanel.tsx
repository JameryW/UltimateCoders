import { useState, useMemo, memo } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn, shortId } from "@/lib/utils";
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

function WorkerDetail({ worker, activeSubtasks }: { worker: WorkerInfo; activeSubtasks: SubtaskSummary[] }) {
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
          <span className="text-[var(--text-muted)] shrink-0 w-20">Active</span>
          <div className="space-y-0.5">
            {activeSubtasks.map((st) => (
              <div key={st.id} className="flex items-center gap-1">
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
}: {
  workers: WorkersData;
  tasks: TasksData;
  stale?: boolean;
}) {
  const [expandedWorkerId, setExpandedWorkerId] = useState<string | null>(null);

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

  const toggleExpand = (workerId: string) => {
    setExpandedWorkerId(expandedWorkerId === workerId ? null : workerId);
  };

  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Workers</CardTitle>
        <Badge variant="ok">
          {workers.available_count}/{workers.total}
        </Badge>
      </CardHeader>

      {!workers.available ? (
        <p className="text-sm text-[var(--text-muted)]"><Badge variant="unavailable">Unavailable</Badge></p>
      ) : workers.workers.length === 0 ? (
        <p className="text-sm text-[var(--text-muted)]">No workers connected</p>
      ) : (
        <ul className="space-y-2">
          {workers.workers.map((w) => {
            const activeSubtasks = workerSubtasks.get(w.id) ?? [];
            const activeCount = activeSubtasks.filter(
              (s) => s.status === "in_progress" || s.status === "assigned"
            ).length;
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
                  <WorkerDetail worker={w} activeSubtasks={activeSubtasks} />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
});
