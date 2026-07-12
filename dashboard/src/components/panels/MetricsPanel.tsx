import { memo, useCallback, useState, useEffect } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { cn, formatUptime, formatNumber } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import { Sparkline } from "@/components/charts/Sparkline";
import { MetricsTrendChart } from "@/components/charts/MetricsTrendChart";
import type { MetricsSnapshot, TaskMetrics, WorkerMetrics, EventMetrics, SystemMetrics, MetricsSample } from "@/types/dashboard";

const TREND_RANGES = [
  { label: "1h", minutes: 60 },
  { label: "6h", minutes: 360 },
  { label: "24h", minutes: 1440 },
] as const;

interface MetricsPanelProps {
  metrics: MetricsSnapshot | null;
  stale?: boolean;
}

// ── Shared sub-components ──────────────────────────────────

function MetricRow({ label, value, unit, className, sparkline }: {
  label: string;
  value: number;
  unit?: string;
  className?: string;
  sparkline?: React.ReactNode;
}) {
  const display = value === 0 && unit ? "—" : `${formatNumber(value)}${unit ?? ""}`;
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-[var(--text-secondary)]">{label}</span>
      <div className="flex items-center gap-2">
        {sparkline}
        <span className={cn("font-mono", className ?? "text-[var(--text-primary)]")}>
          {display}
        </span>
      </div>
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

function DurationMetric({ label, ms, className, sparkline }: {
  label: string;
  ms: number;
  className?: string;
  sparkline?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-[var(--text-secondary)]">{label}</span>
      <div className="flex items-center gap-2">
        {sparkline}
        <span className={cn("font-mono", ms > 0 ? className : "text-[var(--text-muted)]")}>
          {formatDuration(ms)}
        </span>
      </div>
    </div>
  );
}

function SystemOverview({ s, trend }: { s: SystemMetrics; trend: MetricsSample[] }) {
  return (
    <div className="space-y-1">
      <MetricRow label="Uptime" value={s.uptime_seconds} unit="" className="text-cyan-400" />
      <span className="text-xs text-[var(--text-muted)] font-mono">{formatUptime(s.uptime_seconds)}</span>
      <MetricRow label="Rate Limiter" value={Math.round(s.rate_limiter_remaining_ratio * 100)} unit="%" className={pctClass(s.rate_limiter_remaining_ratio * 100, [30, 60])} />
      <MetricRow
        label="Cluster Utilization"
        value={Math.round(s.cluster_utilization_pct)}
        unit="%"
        className={pctClass(s.cluster_utilization_pct)}
        sparkline={<Sparkline data={trend.map((t) => t.cluster_utilization * 100)} color="#f59e0b" />}
      />
    </div>
  );
}

function TaskEfficiency({ t, trend }: { t: TaskMetrics; trend: MetricsSample[] }) {
  return (
    <div className="space-y-1">
      <DurationMetric
        label="Avg Duration"
        ms={t.avg_duration_ms}
        className="text-cyan-400"
        sparkline={<Sparkline data={trend.map((s) => s.avg_duration_ms)} color="#3b82f6" />}
      />
      <MetricRow
        label="Error Rate"
        value={Math.round(trend.length > 0 ? trend[trend.length - 1]!.error_rate * 100 : 0)}
        unit="%"
        className={pctClass(trend.length > 0 ? trend[trend.length - 1]!.error_rate * 100 : 0, [10, 30])}
        sparkline={<Sparkline data={trend.map((s) => s.error_rate * 100)} color="#ef4444" />}
      />
      <DurationMetric label="P50" ms={t.p50_duration_ms} className="text-blue-400" />
      <DurationMetric label="P95" ms={t.p95_duration_ms} className="text-purple-400" />
      <DurationMetric label="P99" ms={t.p99_duration_ms} className="text-fuchsia-400" />
      <MetricRow
        label="Retry Rate"
        value={Math.round(t.retry_rate * 100)}
        unit="%"
        className={t.retry_rate <= 0.1 ? "text-green-400" : t.retry_rate <= 0.3 ? "text-yellow-400" : "text-red-400"}
      />
      <MetricRow label="Slow Tasks" value={t.slow_tasks_count} className={t.slow_tasks_count === 0 ? "text-green-400" : "text-yellow-400"} />
      <MetricRow
        label="Success Rate"
        value={Math.round(t.success_rate * 100)}
        unit="%"
        className={t.success_rate >= 0.8 ? "text-green-400" : t.success_rate >= 0.5 ? "text-yellow-400" : "text-red-400"}
      />
      <div className="flex gap-3 text-xs text-[var(--text-muted)] mt-1">
        <span>✓ {t.total_completed}</span>
        <span>✗ {t.total_failed}</span>
      </div>
    </div>
  );
}

function WorkerPerformance({ w, trend }: { w: WorkerMetrics; trend: MetricsSample[] }) {
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
      <MetricRow
        label="Cluster Load"
        value={Math.round(w.cluster_load_pct)}
        unit="%"
        className={pctClass(w.cluster_load_pct)}
        sparkline={<Sparkline data={trend.map((t) => t.cluster_utilization * 100)} color="#f59e0b" />}
      />

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

function EventFlow({ e, trend }: { e: EventMetrics; trend: MetricsSample[] }) {
  const topTypes = Object.entries(e.event_type_counts).sort((a, b) => b[1] - a[1]).slice(0, 6);
  return (
    <div className="space-y-1">
      <MetricRow
        label="Events/min"
        value={Math.round(e.events_per_minute * 10) / 10}
        className="text-emerald-400"
        sparkline={<Sparkline data={trend.map((t) => t.events_per_minute)} color="#22c55e" />}
      />
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

// ── CSV Export ─────────────────────────────────────────────

function exportMetricsCsv(metrics: MetricsSnapshot) {
  const BOM = "﻿";
  const rows: string[] = [];

  // Section 1: Current snapshot
  rows.push("Section,Metric,Value");
  const t = metrics.task;
  rows.push(`Task,Avg Duration (ms),${t.avg_duration_ms}`);
  rows.push(`Task,P50 Duration (ms),${t.p50_duration_ms}`);
  rows.push(`Task,P95 Duration (ms),${t.p95_duration_ms}`);
  rows.push(`Task,P99 Duration (ms),${t.p99_duration_ms}`);
  rows.push(`Task,Retry Rate,${t.retry_rate}`);
  rows.push(`Task,Slow Tasks,${t.slow_tasks_count}`);
  rows.push(`Task,Total Completed,${t.total_completed}`);
  rows.push(`Task,Total Failed,${t.total_failed}`);
  rows.push(`Task,Success Rate,${t.success_rate}`);
  const s = metrics.system;
  rows.push(`System,Uptime (s),${s.uptime_seconds}`);
  rows.push(`System,Rate Limiter Ratio,${s.rate_limiter_remaining_ratio}`);
  rows.push(`System,Cluster Utilization (%),${s.cluster_utilization_pct}`);
  const w = metrics.worker;
  rows.push(`Worker,Avg Heartbeat Age (s),${w.avg_heartbeat_age_seconds}`);
  rows.push(`Worker,Cluster Load (%),${w.cluster_load_pct}`);
  const e = metrics.event;
  rows.push(`Event,Events/min,${e.events_per_minute}`);
  rows.push(`Event,Error Spike,${e.error_spike}`);

  // Section 2: Trend time series
  rows.push("");
  rows.push("Timestamp,Events/min,Avg Duration (ms),Error Rate,Cluster Utilization");
  for (const sample of metrics.trend) {
    const ts = new Date(sample.timestamp * 1000).toISOString();
    rows.push(`${ts},${sample.events_per_minute},${sample.avg_duration_ms},${sample.error_rate},${sample.cluster_utilization}`);
  }

  const csv = BOM + rows.join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `metrics-${new Date().toISOString().slice(0, 19).replace(/:/g, "-")}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Main panel ─────────────────────────────────────────────

export const MetricsPanel = memo(function MetricsPanel({ metrics, stale }: MetricsPanelProps) {
  const [trendRange, setTrendRange] = useState<number>(60); // minutes
  const [extendedTrend, setExtendedTrend] = useState<MetricsSample[] | null>(null);

  const handleExport = useCallback(() => {
    if (metrics) exportMetricsCsv(metrics);
  }, [metrics]);

  // Fetch extended trend when range > 60min (SSE only provides last 1h)
  useEffect(() => {
    if (trendRange <= 60 || !metrics) return;
    let cancelled = false;
    fetch(`/dashboard/api/trend?minutes=${trendRange}`)
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (!cancelled && data?.trend) {
          setExtendedTrend(data.trend);
        }
      })
      .catch(() => { /* graceful fallback */ });
    return () => { cancelled = true; };
  }, [trendRange, metrics]);

  // Reset extended trend when switching back to 1h (SSE data is sufficient)
  const effectiveTrend = trendRange <= 60 ? null : extendedTrend;

  if (!metrics) {
    return (
      <Card className={cn(stale && "opacity-70")}>
        <CardHeader>
          <CardTitle>Metrics</CardTitle>
        </CardHeader>
        <EmptyState icon="activity" title="Waiting for metrics data…" />
      </Card>
    );
  }

  // Use extended trend from API when range > 60min, otherwise SSE trend
  const trend = effectiveTrend ?? (metrics.trend ?? []);

  return (
    <div className={cn("space-y-4", stale && "opacity-70")}>
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-[var(--text-primary)]">Metrics</h3>
        <div className="flex items-center gap-2">
          <div className="flex rounded-md border border-[var(--border-color)] overflow-hidden">
            {TREND_RANGES.map(r => (
              <button
                key={r.minutes}
                onClick={() => setTrendRange(r.minutes)}
                className={cn(
                  "px-2 py-0.5 text-xs transition-colors cursor-pointer",
                  trendRange === r.minutes
                    ? "bg-[var(--bg-surface-alt)] text-[var(--text-primary)] font-medium"
                    : "text-[var(--text-muted)] hover:text-[var(--text-primary)]",
                )}
              >
                {r.label}
              </button>
            ))}
          </div>
          <button
            onClick={handleExport}
            className="text-xs px-2.5 py-1 rounded-md border border-[var(--border-color)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-surface-alt)] transition-colors cursor-pointer"
          >
            Export CSV
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* System Overview */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">System Overview</CardTitle>
          </CardHeader>
          <div className="px-4 pb-4">
            <SystemOverview s={metrics.system} trend={trend} />
          </div>
        </Card>

        {/* Task Efficiency */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Task Efficiency</CardTitle>
          </CardHeader>
          <div className="px-4 pb-4">
            <TaskEfficiency t={metrics.task} trend={trend} />
          </div>
        </Card>

        {/* Worker Performance */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Worker Performance</CardTitle>
          </CardHeader>
          <div className="px-4 pb-4">
            <WorkerPerformance w={metrics.worker} trend={trend} />
          </div>
        </Card>

        {/* Event Flow */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Event Flow</CardTitle>
          </CardHeader>
          <div className="px-4 pb-4">
            <EventFlow e={metrics.event} trend={trend} />
          </div>
        </Card>
      </div>

      {/* Full-width trend chart */}
      <MetricsTrendChart trend={trend} stale={stale} />
    </div>
  );
});
