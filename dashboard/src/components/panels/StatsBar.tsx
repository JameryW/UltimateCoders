import { useMemo, memo } from "react";
import type { TasksData, WorkersData, DashboardEvent } from "@/types/dashboard";
import { cn } from "@/lib/utils";

interface StatsBarProps {
  tasks: TasksData;
  workers: WorkersData;
  eventLog: DashboardEvent[];
  stale?: boolean;
}

export const StatsBar = memo(function StatsBar({ tasks, workers, eventLog, stale }: StatsBarProps) {
  const stats = useMemo(() => {
    if (!tasks.available) return null;

    const total = tasks.total;
    const completed = tasks.status_counts["completed"] ?? 0;
    const successRate = total > 0 ? Math.round((completed / total) * 100) : 0;

    // ponytail: derive avg duration from eventLog — completed events with duration_ms
    const completedEvents = eventLog.filter(
      (e) => e.type === "task_completed" && typeof e.details.duration_ms === "number"
    );
    const avgDurationMs =
      completedEvents.length > 0
        ? completedEvents.reduce((sum, e) => sum + (e.details.duration_ms as number), 0) /
          completedEvents.length
        : undefined;

    const activeWorkers = workers.available ? workers.available_count : 0;

    return { total, completed, successRate, avgDurationMs, activeWorkers };
  }, [tasks, workers, eventLog]);

  if (!stats) return null;

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
      label: "Avg Duration",
      value: stats.avgDurationMs != null ? formatDuration(stats.avgDurationMs) : "—",
      subtitle: stats.avgDurationMs == null ? "No completed tasks yet" : undefined,
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      ),
      accent: "text-cyan-400",
    },
    {
      label: "Active Workers",
      value: stats.activeWorkers,
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
        </svg>
      ),
      accent: "text-purple-400",
    },
  ];

  return (
    <div className={cn("grid grid-cols-2 lg:grid-cols-4 gap-4", stale && "opacity-70")}>
      {cards.map((c) => (
        <div
          key={c.label}
          className="rounded-xl border border-[var(--border-color)] bg-[var(--bg-surface)] p-4 flex items-center gap-3 transition-all duration-200 hover:shadow-sm"
        >
          <div className={cn(c.accent, "shrink-0")}>{c.icon}</div>
          <div>
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
