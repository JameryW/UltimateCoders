import { useState, useEffect, useRef } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn, truncate, shortId } from "@/lib/utils";
import { TaskDetail } from "@/components/panels/TaskDetail";
import type { TasksData, TaskEvent } from "@/types/dashboard";

function statusBadgeClass(status: string): string {
  switch (status) {
    case "completed": return "bg-green-900/50 text-green-300";
    case "failed": return "bg-red-900/50 text-red-300";
    case "paused": return "bg-yellow-900/50 text-yellow-300";
    case "in_progress": return "bg-blue-900/50 text-blue-300";
    default: return "bg-gray-800 text-gray-400";
  }
}

function statusBorderColor(status: string): string {
  switch (status) {
    case "completed": return "border-l-green-500";
    case "failed": return "border-l-red-500";
    case "paused": return "border-l-yellow-500";
    case "in_progress": return "border-l-blue-500";
    default: return "border-l-gray-600";
  }
}

interface TasksPanelProps {
  data: TasksData;
  interactionLog: Record<string, TaskEvent[]>;
  onFlush?: () => void;
  onPauseTask?: (taskId: string) => void;
  onResumeTask?: (taskId: string) => void;
  stale?: boolean;
  /** Task ID to highlight and auto-scroll to (set after submit). */
  highlightTaskId?: string | null;
  /** Callback to clear highlight after it's been shown. */
  onHighlightShown?: () => void;
}

export function TasksPanel({ data, interactionLog, onFlush, onPauseTask, onResumeTask, stale, highlightTaskId, onHighlightShown }: TasksPanelProps) {
  const [expandedTaskId, setExpandedTaskId] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const highlightRef = useRef<HTMLLIElement>(null);

  // ponytail: auto-expand and scroll to highlighted task after submit
  useEffect(() => {
    if (highlightTaskId) {
      setExpandedTaskId(highlightTaskId);
      setStatusFilter(null); // clear filter so the task is visible
      requestAnimationFrame(() => {
        highlightRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
      onHighlightShown?.();
    }
  }, [highlightTaskId, onHighlightShown]);

  const toggleExpand = (taskId: string) => {
    setExpandedTaskId(expandedTaskId === taskId ? null : taskId);
  };

  return (
    <Card className="md:col-span-2" stale={stale}>
      <CardHeader>
        <CardTitle>Tasks</CardTitle>
        <div className="flex items-center gap-2">
          <Badge>{data.total}</Badge>
          {data.pending_task_count > 0 && onFlush && (
            <button
              onClick={onFlush}
              aria-label="Flush pending tasks"
              className="bg-blue-900/50 text-blue-300 hover:bg-blue-900/70 px-2 py-0.5 rounded text-xs cursor-pointer"
            >
              Flush
            </button>
          )}
        </div>
      </CardHeader>

      {!data.available ? (
        <p className="text-sm text-gray-500">Tasks not available</p>
      ) : (
        <>
          {Object.keys(data.status_counts).length > 0 && (
            <div className="flex flex-wrap gap-1.5 mb-2">
              {Object.entries(data.status_counts).map(([status, count]) => (
                <button
                  key={status}
                  onClick={() => setStatusFilter(statusFilter === status ? null : status)}
                  className={cn(
                    "text-xs px-1.5 py-0.5 rounded cursor-pointer",
                    statusBadgeClass(status),
                    statusFilter === status && "ring-1 ring-white/50"
                  )}
                >
                  {status}: {count}
                </button>
              ))}
            </div>
          )}

          {data.pending_task_count > 0 && (
            <p className="text-xs text-yellow-400 mb-2">
              {data.pending_task_count} pending task{data.pending_task_count !== 1 ? "s" : ""}
            </p>
          )}

          <ul className="space-y-1.5 max-h-[600px] overflow-y-auto" aria-label="Task list">
            {data.tasks
              .filter((task) => !statusFilter || task.status === statusFilter)
              .map((task) => (
              <li key={task.id} ref={task.id === highlightTaskId ? highlightRef : undefined}>
                <div
                  role="button"
                  tabIndex={0}
                  aria-expanded={expandedTaskId === task.id}
                  aria-label={`${task.description}, status ${task.status}`}
                  className={cn(
                    "border-l-2 pl-2 py-1 cursor-pointer hover:bg-dark-700/50 rounded-r",
                    statusBorderColor(task.status),
                    task.id === highlightTaskId && "ring-1 ring-blue-400/50 bg-blue-900/20"
                  )}
                  onClick={() => toggleExpand(task.id)}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleExpand(task.id); } }}
                >
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-gray-300">
                      {truncate(task.description, 40)}
                    </span>
                    <div className="flex items-center gap-1.5">
                      {task.status === "in_progress" && onPauseTask && (
                        <button
                          onClick={(e) => { e.stopPropagation(); onPauseTask(task.id); }}
                          aria-label={`Pause task ${shortId(task.id)}`}
                          className="bg-yellow-900/50 text-yellow-300 hover:bg-yellow-900/70 px-2 py-0.5 rounded text-xs cursor-pointer"
                        >
                          Pause
                        </button>
                      )}
                      {task.status === "paused" && onResumeTask && (
                        <button
                          onClick={(e) => { e.stopPropagation(); onResumeTask(task.id); }}
                          aria-label={`Resume task ${shortId(task.id)}`}
                          className="bg-green-900/50 text-green-300 hover:bg-green-900/70 px-2 py-0.5 rounded text-xs cursor-pointer"
                        >
                          Resume
                        </button>
                      )}
                      <span
                        className={cn("text-xs px-1.5 py-0.5 rounded", statusBadgeClass(task.status))}
                      >
                        {task.status}
                      </span>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 text-xs text-gray-500 mt-0.5">
                    <span className="font-mono">{shortId(task.id)}</span>
                    {task.project_id && <span>proj: {shortId(task.project_id)}</span>}
                  </div>
                </div>

                {/* Detail expansion */}
                {expandedTaskId === task.id && (
                  <TaskDetail
                    task={task}
                    interactionLog={interactionLog[task.id] ?? []}
                  />
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}
