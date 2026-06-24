import { useMemo, memo } from "react";
import type { TasksData, WorkersData, DashboardEvent } from "@/types/dashboard";
import { cn, percentile } from "@/lib/utils";

const ONE_HOUR_MS = 3600_000;

interface StatsBarProps {
  tasks: TasksData;
  workers: WorkersData;
  eventLog: DashboardEvent[];
  stale?: boolean;
}

export const StatsBar = memo(function StatsBar({ tasks, workers, eventLog, stale }: StatsBarProps) {
  const stats = useMemo(() => {
    const now = Date.now();
    const cutoff = now - ONE_HOUR_MS;

    // Recent events in last 1h
    const recentEvents = eventLog.filter(
      (e) => new Date(e.timestamp).getTime() >= cutoff
    );
    const completed1h = recentEvents.filter((e) => e.type === "task_completed" || e.type === "subtask_completed");
    const failed1h = recentEvents.filter((e) => e.type === "task_failed" || e.type === "subtask_failed");

    // Throughput: completed tasks/subtasks per hour
    const throughput = completed1h.length;

    // Error rate: failed / (completed + failed) in last 1h
    const total1h = completed1h.length + failed1h.length;
    const errorRate = total1h > 0 ? Math.round((failed1h.length / total1h) * 100) : 0;

    // Latency P95 from completed events with duration_ms
    const durations = completed1h
      .map((e) => e.details.duration_ms)
      .filter((d): d is number => typeof d === "number" && d > 0)
      .sort((a, b) => a - b);
    const p95 = durations.length >= 3 ? percentile(durations, 95) : undefined;

    // Overall success rate (all time)
    const total = tasks.available ? tasks.total : 0;
    const completed = tasks.available ? (tasks.status_counts["completed"] ?? 0) : 0;
    const successRate = total > 0 ? Math.round((completed / total) * 100) : 0;

    // Cluster load
    const totalLoad = workers.available
      ? workers.workers.reduce((sum, w) => sum + w.current_load, 0)
      : 0;
    const totalCapacity = workers.available
      ? workers.workers.reduce((sum, w) => sum + w.max_capacity, 0)
      : 0;
    const clusterLoadPct = totalCapacity > 0
      ? Math.round((totalLoad / totalCapacity) * 100)
      : 0;
    const activeWorkers = workers.available ? workers.available_count : 0;

    return { total, successRate, throughput, errorRate, p95, clusterLoadPct, activeWorkers, totalLoad, totalCapacity };
  }, [tasks, workers, eventLog]);

  if (!tasks.available && !workers.available) return null;

  const cards = [
    {
      label: "Total Tasks",
      value: stats.total,
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
        </svg>
      ),
      accent: "text-blue-400",
    },
    {
      label: "Success Rate",
      value: `${stats.successRate}%`,
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      ),
      accent: stats.successRate >= 80 ? "text-green-400" : stats.successRate >= 50 ? "text-yellow-400" : "text-red-400",
    },
    {
      label: "Throughput",
      value: `${stats.throughput}/h`,
      subtitle: "last 1h",
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
        </svg>
      ),
      accent: "text-emerald-400",
    },
    {
      label: "Error Rate",
      value: `${stats.errorRate}%`,
      subtitle: "last 1h",
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
        </svg>
      ),
      accent: stats.errorRate <= 10 ? "text-green-400" : stats.errorRate <= 30 ? "text-yellow-400" : "text-red-400",
    },
    {
      label: "Latency P95",
      value: stats.p95 != null ? formatDuration(stats.p95) : "—",
      subtitle: stats.p95 == null ? "Need 3+ samples" : undefined,
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      ),
      accent: "text-cyan-400",
    },
    {
      label: "Cluster Load",
      value: `${stats.clusterLoadPct}%`,
      subtitle: `${stats.totalLoad}/${stats.totalCapacity}`,
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
        </svg>
      ),
      accent: stats.clusterLoadPct <= 60 ? "text-green-400" : stats.clusterLoadPct <= 85 ? "text-yellow-400" : "text-red-400",
    },
  ];

  return (
    <div className={cn("grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4", stale && "opacity-70")}>
      {cards.map((c) => (
        <div
          key={c.label}
          className="rounded-xl border border-[var(--border-color)] bg-[var(--bg-surface)] p-4 flex items-center gap-3 transition-all duration-200 hover:shadow-sm"
        >
          <div className={cn(c.accent, "shrink-0")}>{c.icon}</div>
          <div className="min-w-0">
            <p className="text-xs text-[var(--text-muted)]">{c.label}</p>
            <p className={cn("text-lg font-semibold", c.accent)}>{c.value}</p>
            {c.subtitle && <p className="text-[10px] text-[var(--text-muted)]">{c.subtitle}</p>}
          </div>
        </div>
      ))}
    </div>
  );
});

// ponytail: simple duration formatter — ms to human readable
function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  if (m < 60) return `${m}m ${sec}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
