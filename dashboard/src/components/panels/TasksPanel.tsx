import { useState, useEffect, useRef } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn, truncate, shortId, statusBadgeClass } from "@/lib/utils";
import { showToast } from "@/components/ui/toast";
import { TaskDetail } from "@/components/panels/TaskDetail";
import type { FileBrowserNavigateEvent } from "@/components/panels/FileBrowser";
import type { GrpcSubmitResult } from "@/hooks/useGrpcWeb";
import type { TasksData, TaskEvent } from "@/types/dashboard";

function statusBorderColor(status: string): string {
  switch (status) {
    case "completed": return "border-l-green-500";
    case "failed": return "border-l-red-500";
    case "paused": return "border-l-yellow-500";
    case "in_progress": return "border-l-blue-500";
    default: return "border-[var(--border-color)]";
  }
}

interface TasksPanelProps {
  data: TasksData;
  interactionLog: Record<string, TaskEvent[]>;
  onFlush?: () => void;
  onPauseTask?: (taskId: string) => void;
  onResumeTask?: (taskId: string) => void;
  stale?: boolean;
  highlightTaskId?: string | null;
  onHighlightShown?: () => void;
  onNavigateFile?: (nav: FileBrowserNavigateEvent) => void;
  grpcSubmitTask?: (description: string, projectId: string) => Promise<GrpcSubmitResult>;
  onTaskCreated?: (taskId: string) => void;
  onOptimisticAdd?: (taskId: string, description: string, projectId: string, subtaskCount: number, subtasks?: Array<{ id: string; description: string; status: string; dependsOn: string[] }>) => void;
}

export function TasksPanel({ data, interactionLog, onFlush, onPauseTask, onResumeTask, stale, highlightTaskId, onHighlightShown, onNavigateFile, grpcSubmitTask, onTaskCreated, onOptimisticAdd }: TasksPanelProps) {
  const [expandedTaskId, setExpandedTaskId] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const [submitDesc, setSubmitDesc] = useState("");
  const [submitProj, setSubmitProj] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const highlightRef = useRef<HTMLLIElement>(null);

  // ponytail: auto-expand and scroll to highlighted task after submit
  useEffect(() => {
    if (highlightTaskId) {
      setExpandedTaskId(highlightTaskId);
      setStatusFilter(null); // clear filter so the task is visible
      // Double rAF to wait for DOM render after state changes
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          highlightRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
        });
      });
      onHighlightShown?.();
    }
  }, [highlightTaskId, onHighlightShown]);

  const toggleExpand = (taskId: string) => {
    setExpandedTaskId(expandedTaskId === taskId ? null : taskId);
  };

  const handleInlineSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const desc = submitDesc.trim();
    if (!desc || !grpcSubmitTask) return;
    setSubmitting(true);
    try {
      const resp = await grpcSubmitTask(desc, submitProj.trim());
      if (resp.success) {
        showToast(resp.subtaskCount > 0 ? `Task ${shortId(resp.taskId)} — ${resp.subtaskCount} subtask${resp.subtaskCount > 1 ? "s" : ""}` : `Task ${shortId(resp.taskId)} submitted`, "success");
        onOptimisticAdd?.(resp.taskId, desc, submitProj.trim(), resp.subtaskCount, resp.subtasks);
        setSubmitDesc("");
        setSubmitProj("");
        onTaskCreated?.(resp.taskId);
      } else {
        showToast(`Submit failed: ${resp.status}`, "error");
      }
    } catch (err) {
      showToast(`Submit failed: ${String(err)}`, "error");
    } finally {
      setSubmitting(false);
    }
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
              className="btn-action-info px-2 py-0.5 rounded text-xs cursor-pointer"
            >
              Flush
            </button>
          )}
        </div>
      </CardHeader>

      {/* Inline task submit form — always visible when gRPC connected */}
      {grpcSubmitTask && (
        <form onSubmit={handleInlineSubmit} className="flex gap-2 mb-3">
          <input
            value={submitDesc}
            onChange={(e) => setSubmitDesc(e.target.value)}
            placeholder="New task..."
            aria-label="Task description"
            className="flex-1 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm text-[var(--text-primary)] focus:border-blue-500 focus:outline-none"
          />
          <input
            value={submitProj}
            onChange={(e) => setSubmitProj(e.target.value)}
            placeholder="Project"
            aria-label="Project ID"
            className="w-24 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm text-[var(--text-primary)] focus:border-blue-500 focus:outline-none"
          />
          <button
            type="submit"
            disabled={submitting || !submitDesc.trim()}
            className="btn-action-info px-3 py-2 rounded-md text-xs font-medium cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
          >
            {submitting ? "..." : "Submit"}
          </button>
        </form>
      )}

      {!data.available ? (
        <p className="text-sm text-[var(--text-muted)]"><Badge variant="unavailable">Unavailable</Badge></p>
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
                    statusFilter === status && "ring-1 ring-[var(--text-muted)]"
                  )}
                >
                  {status}: {count}
                </button>
              ))}
            </div>
          )}

          {data.pending_task_count > 0 && (
            <p className="text-xs text-yellow-500 mb-2">
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
                    "border-l-2 pl-2 py-1 cursor-pointer hover:bg-[var(--bg-surface-alt)]/50 rounded-r",
                    statusBorderColor(task.status),
                    task.id === highlightTaskId && "highlight-ring"
                  )}
                  onClick={() => toggleExpand(task.id)}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleExpand(task.id); } }}
                >
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-[var(--text-primary)]">
                      {truncate(task.description, 40)}
                    </span>
                    <div className="flex items-center gap-1.5">
                      {task.status === "in_progress" && onPauseTask && (
                        <button
                          onClick={(e) => { e.stopPropagation(); onPauseTask(task.id); }}
                          aria-label={`Pause task ${shortId(task.id)}`}
                          className="btn-action-warn px-2 py-0.5 rounded text-xs cursor-pointer"
                        >
                          Pause
                        </button>
                      )}
                      {task.status === "paused" && onResumeTask && (
                        <button
                          onClick={(e) => { e.stopPropagation(); onResumeTask(task.id); }}
                          aria-label={`Resume task ${shortId(task.id)}`}
                          className="btn-action-ok px-2 py-0.5 rounded text-xs cursor-pointer"
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
                  <div className="flex items-center gap-2 text-xs text-[var(--text-muted)] mt-0.5">
                    <span className="font-mono">{shortId(task.id)}</span>
                    {task.project_id && <span>proj: {shortId(task.project_id)}</span>}
                  </div>
                </div>

                {/* Detail expansion */}
                {expandedTaskId === task.id && (
                  <TaskDetail
                    task={task}
                    interactionLog={interactionLog[task.id] ?? []}
                    onNavigateFile={onNavigateFile}
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
