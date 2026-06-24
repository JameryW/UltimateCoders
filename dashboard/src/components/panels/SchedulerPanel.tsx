import { memo } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn, shortId } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import type { SchedulerData } from "@/types/dashboard";

function schedulerStatusBadge(isRunning: boolean): "ok" | "degraded" | "unavailable" {
  if (isRunning) return "ok";
  return "degraded";
}

function executionStatusColor(status: string): string {
  switch (status) {
    case "completed":
      return "text-green-400";
    case "failed":
      return "text-red-400";
    case "in_progress":
      return "text-blue-400";
    default:
      return "text-[var(--text-secondary)]";
  }
}

// ponytail: format relative time from ISO timestamp
function relativeTime(ts: string): string {
  const diff = Date.now() - new Date(ts).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

// ponytail: format duration between two ISO timestamps
function formatDuration(start: string, end: string): string {
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

interface SchedulerPanelProps {
  data: SchedulerData;
  onTriggerJob?: (jobId: string) => void;
  stale?: boolean;
  embedded?: boolean;
}

export const SchedulerPanel = memo(function SchedulerPanel({ data, onTriggerJob, stale, embedded }: SchedulerPanelProps) {
  if (!data.available) {
    const unavailable = <EmptyState icon="clock" title="Scheduler not available" description="The scheduler endpoint is unreachable" />;
    if (embedded) return unavailable;
    return (
      <Card stale={stale}>
        <CardHeader>
          <CardTitle>Scheduler</CardTitle>
          <Badge variant="unavailable">Not Available</Badge>
        </CardHeader>
        {unavailable}
      </Card>
    );
  }

  const content = (
    <>
      {data.night_window && (
        <div className="flex items-center gap-2 mb-3 text-sm">
          <span className="text-[var(--text-secondary)]">Night Window:</span>
          <span
            className={cn(
              "text-xs px-1.5 py-0.5 rounded",
              data.night_window.is_active
                ? "status-paused"
                : "status-completed"
            )}
          >
            {data.night_window.is_active ? "ACTIVE" : "INACTIVE"}
          </span>
          <span className="text-xs text-[var(--text-muted)]">
            {data.night_window.start} - {data.night_window.end}
          </span>
        </div>
      )}

      {data.jobs.length > 0 && (
        <ul className="space-y-1.5 mb-3">
          {data.jobs.map((job) => (
            <li key={job.id} className="flex items-center justify-between text-sm">
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    "w-2 h-2 rounded-full",
                    job.enabled ? "bg-green-500" : "bg-[var(--bg-surface-alt)]"
                  )}
                />
                <span className="text-[var(--text-primary)]">{job.description}</span>
              </div>
              <div className="flex items-center gap-2">
                {job.cron_expression && (
                  <span className="text-xs text-[var(--text-muted)] font-mono">
                    {job.cron_expression}
                  </span>
                )}
                {onTriggerJob && (
                  <button
                    onClick={() => onTriggerJob(job.id)}
                    className="btn-action-info px-2 py-0.5 rounded text-xs cursor-pointer"
                  >
                    Trigger
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {data.execution_history.length > 0 && (
        <div className="border-t border-[var(--border-color)] pt-2">
          <p className="text-xs text-[var(--text-muted)] font-medium mb-1.5">
            Execution History
          </p>
          <ul className="space-y-1">
            {data.execution_history.map((exec, i) => (
              <li key={exec.started_at ?? `${exec.task_id}-${i}`} className="flex items-center gap-2 text-xs">
                <span className="font-mono text-[var(--text-secondary)]">{shortId(exec.task_id)}</span>
                <span className={executionStatusColor(exec.status)}>{exec.status}</span>
                {exec.started_at && (
                  <span className="text-[var(--text-muted)]">{relativeTime(exec.started_at)}</span>
                )}
                {exec.started_at && exec.completed_at && (
                  <span className="text-[var(--text-muted)]">({formatDuration(exec.started_at, exec.completed_at)})</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );

  if (embedded) return content;

  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Scheduler</CardTitle>
        <Badge variant={schedulerStatusBadge(data.is_running)}>
          {data.is_running ? "RUNNING" : "STOPPED"}
        </Badge>
      </CardHeader>
      {content}
    </Card>
  );
});
