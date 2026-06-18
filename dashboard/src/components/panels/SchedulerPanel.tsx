import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn, shortId } from "@/lib/utils";
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

interface SchedulerPanelProps {
  data: SchedulerData;
  onTriggerJob?: (jobId: string) => void;
  stale?: boolean;
}

export function SchedulerPanel({ data, onTriggerJob, stale }: SchedulerPanelProps) {
  if (!data.available) {
    return (
      <Card stale={stale}>
        <CardHeader>
          <CardTitle>Scheduler</CardTitle>
          <Badge variant="unavailable">Not Available</Badge>
        </CardHeader>
        <p className="text-sm text-[var(--text-muted)]">Scheduler not available</p>
      </Card>
    );
  }

  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Scheduler</CardTitle>
        <Badge variant={schedulerStatusBadge(data.is_running)}>
          {data.is_running ? "RUNNING" : "STOPPED"}
        </Badge>
      </CardHeader>

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
            {data.night_window.start} - {data.night_window.end} ({data.night_window.timezone})
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
          <p className="text-xs text-[var(--text-muted)] uppercase tracking-wide mb-1.5">
            Execution History
          </p>
          <ul className="space-y-1">
            {data.execution_history.map((exec, i) => (
              <li key={`${exec.task_id}-${i}`} className="flex items-center gap-2 text-xs">
                <span className="font-mono text-[var(--text-secondary)]">{shortId(exec.task_id)}</span>
                <span className={executionStatusColor(exec.status)}>{exec.status}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
