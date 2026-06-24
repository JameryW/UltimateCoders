import { memo } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { cn, formatUptime, formatNumber } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import type { MetricsSnapshot, TaskMetrics, WorkerMetrics, EventMetrics, SystemMetrics } from "@/types/dashboard";

interface MetricsPanelProps {
  metrics: MetricsSnapshot | null;
  stale?: boolean;
}

// ── Shared sub-components ──────────────────────────────────

function MetricRow({ label, value, unit, className }: {
  label: string;
  value: number;
  unit?: string;
  className?: string;
}) {
  const display = value === 0 && unit ? "—" : `${formatNumber(value)}${unit ?? ""}`;
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-[var(--text-secondary)]">{label}</span>
      <span className={cn("font-mono", className ?? "text-[var(--text-primary)]")}>
        {display}
      </span>
    </div>
  );
}

function formatDuration(ms: number): string {
  if (!ms) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  if (m < 60) return `${m}m ${sec}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function pctClass(pct: number, thresholds = [60, 85]): string {
  if (pct <= thresholds[0]) return "text-green-400";
  if (pct <= thresholds[1]) return "text-yellow-400";
  return "text-red-400";
}

// ── Section blocks ─────────────────────────────────────────

function DurationMetric({ label, ms, className }: {
  label: string;
  ms: number;
  className?: string;
}) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-[var(--text-secondary)]">{label}</span>
      <span className={cn("font-mono", ms > 0 ? className : "text-[var(--text-muted)]")}>
        {formatDuration(ms)}
      </span>
    </div>
  );
}

function SystemOverview({ s }: { s: SystemMetrics }) {
  return (
    <div className="space-y-1">
      <MetricRow label="Uptime" value={s.uptime_seconds} unit="" className="text-cyan-400" />
      <span className="text-xs text-[var(--text-muted)] font-mono">{formatUptime(s.uptime_seconds)}</span>
      <div className="flex items-center justify-between text-sm">
        <span className="text-[var(--text-secondary)]">Circuit Breaker</span>
        <span className={cn("font-mono", "text-xs px-1.5 py-0.5 rounded", s.circuit_breaker_state === "closed" ? "bg-green-900/30 text-green-400" : s.circuit_breaker_state === "open" ? "bg-red-900/30 text-red-400" : "bg-yellow-900/30 text-yellow-400")}>
          {s.circuit_breaker_state}
        </span>
      </div>
      <MetricRow label="Rate Limiter" value={Math.round(s.rate_limiter_remaining_ratio * 100)} unit="%" className={pctClass(s.rate_limiter_remaining_ratio * 100, [30, 60])} />
      <MetricRow label="Cluster Utilization" value={Math.round(s.cluster_utilization_pct)} unit="%" className={pctClass(s.cluster_utilization_pct)} />
    </div>
  );
}

function TaskEfficiency({ t }: { t: TaskMetrics }) {
  return (
    <div className="space-y-1">
      <DurationMetric label="Avg Duration" ms={t.avg_duration_ms} className="text-cyan-400" />
      <DurationMetric label="P50" ms={t.p50_duration_ms} className="text-blue-400" />
      <DurationMetric label="P95" ms={t.p95_duration_ms} className="text-purple-400" />
      <DurationMetric label="P99" ms={t.p99_duration_ms} className="text-fuchsia-400" />
      <MetricRow label="Retry Rate" value={Math.round(t.retry_rate * 100)} unit="%" className={t.retry_rate <= 0.1 ? "text-green-400" : t.retry_rate <= 0.3 ? "text-yellow-400" : "text-red-400"} />
      <MetricRow label="Slow Tasks" value={t.slow_tasks_count} className={t.slow_tasks_count === 0 ? "text-green-400" : "text-yellow-400"} />
      <MetricRow label="Success Rate" value={Math.round(t.success_rate * 100)} unit="%" className={t.success_rate >= 0.8 ? "text-green-400" : t.success_rate >= 0.5 ? "text-yellow-400" : "text-red-400"} />
      <div className="flex gap-3 text-xs text-[var(--text-muted)] mt-1">
        <span>✓ {t.total_completed}</span>
        <span>✗ {t.total_failed}</span>
      </div>
    </div>
  );
}

function WorkerPerformance({ w }: { w: WorkerMetrics }) {
  const toolEntries = Object.entries(w.per_worker_tool_calls).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const subtaskEntries = Object.entries(w.per_worker_subtask_count).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const maxTools = Math.max(1, ...toolEntries.map(([, v]) => v));
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-sm">
        <span className="text-[var(--text-secondary)]">Avg Heartbeat Age</span>
        <span className={cn("font-mono", w.avg_heartbeat_age_seconds > 0 ? "text-cyan-400" : "text-[var(--text-muted)]")}>
          {w.avg_heartbeat_age_seconds > 0 ? `${w.avg_heartbeat_age_seconds.toFixed(1)}s` : "—"}
        </span>
      </div>
      <MetricRow label="Cluster Load" value={Math.round(w.cluster_load_pct)} unit="%" className={pctClass(w.cluster_load_pct)} />

      {toolEntries.length > 0 && (
        <div className="mt-2">
          <p className="text-xs text-[var(--text-muted)] font-medium mb-1">Tool Calls (1h)</p>
          <div className="space-y-0.5">
            {toolEntries.map(([id, count]) => (
              <div key={id} className="flex items-center gap-2 text-xs">
                <span className="text-[var(--text-secondary)] w-16 truncate" title={id}>{id}</span>
                <div className="flex-1 bg-[var(--border-color)] rounded h-1.5 overflow-hidden">
                  <div className="bg-blue-500 h-full rounded" style={{ width: `${(count / maxTools) * 100}%` }} />
                </div>
                <span className="font-mono text-[var(--text-primary)] w-8 text-right">{count}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {subtaskEntries.length > 0 && (
        <div className="mt-2">
          <p className="text-xs text-[var(--text-muted)] font-medium mb-1">Subtasks (1h)</p>
          <div className="space-y-0.5">
            {subtaskEntries.map(([id, count]) => (
              <div key={id} className="flex items-center justify-between text-xs">
                <span className="text-[var(--text-secondary)] truncate" title={id}>{id}</span>
                <span className="font-mono text-[var(--text-primary)]">{count}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function EventFlow({ e }: { e: EventMetrics }) {
  const topTypes = Object.entries(e.event_type_counts).sort((a, b) => b[1] - a[1]).slice(0, 6);
  return (
    <div className="space-y-1">
      <MetricRow label="Events/min" value={Math.round(e.events_per_minute * 10) / 10} className="text-emerald-400" />
      <div className="flex items-center justify-between text-sm">
        <span className="text-[var(--text-secondary)]">Error Spike</span>
        <span className={cn("font-mono text-xs px-1.5 py-0.5 rounded", e.error_spike ? "bg-red-900/30 text-red-400" : "bg-green-900/30 text-green-400")}>
          {e.error_spike ? "⚠ DETECTED" : "✓ Normal"}
        </span>
      </div>
      {topTypes.length > 0 && (
        <div className="mt-2">
          <p className="text-xs text-[var(--text-muted)] font-medium mb-1">Event Types (1h)</p>
          <div className="space-y-0.5">
            {topTypes.map(([type, count]) => (
              <div key={type} className="flex items-center justify-between text-xs">
                <span className="text-[var(--text-secondary)]">{type.replace(/_/g, " ")}</span>
                <span className="font-mono text-[var(--text-primary)]">{count}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Main panel ─────────────────────────────────────────────

export const MetricsPanel = memo(function MetricsPanel({ metrics, stale }: MetricsPanelProps) {
  if (!metrics) {
    return (
      <Card className={cn(stale && "opacity-70")}>
        <CardHeader>
          <CardTitle>Metrics</CardTitle>
        </CardHeader>
        <EmptyState message="Waiting for metrics data…" />
      </Card>
    );
  }

  return (
    <div className={cn("grid grid-cols-1 md:grid-cols-2 gap-4", stale && "opacity-70")}>
      {/* System Overview */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">System Overview</CardTitle>
        </CardHeader>
        <div className="px-4 pb-4">
          <SystemOverview s={metrics.system} />
        </div>
      </Card>

      {/* Task Efficiency */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Task Efficiency</CardTitle>
        </CardHeader>
        <div className="px-4 pb-4">
          <TaskEfficiency t={metrics.task} />
        </div>
      </Card>

      {/* Worker Performance */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Worker Performance</CardTitle>
        </CardHeader>
        <div className="px-4 pb-4">
          <WorkerPerformance w={metrics.worker} />
        </div>
      </Card>

      {/* Event Flow */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Event Flow</CardTitle>
        </CardHeader>
        <div className="px-4 pb-4">
          <EventFlow e={metrics.event} />
        </div>
      </Card>
    </div>
  );
});
