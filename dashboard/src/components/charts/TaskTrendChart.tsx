import { useMemo } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  Legend,
} from "recharts";
import type { TasksData, DashboardEvent } from "@/types/dashboard";
import { cn } from "@/lib/utils";

interface TaskTrendChartProps {
  tasks: TasksData;
  eventLog: DashboardEvent[];
  stale?: boolean;
}

/** Truncate a timestamp to the start of its hour, e.g. "2026-06-18T14:00:00". */
function toHourBucket(ts: number): string {
  const d = new Date(ts);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}T${hh}:00`;
}

/** Format an hour-bucket key for display on the X axis, e.g. "Jun 18 14:00". */
function formatHourLabel(bucketKey: string): string {
  const d = new Date(bucketKey);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
    d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Bucket events by hour (last 24h) and count completions vs failures. */
function bucketByHour(events: DashboardEvent[]): { name: string; completed: number; failed: number }[] {
  const now = Date.now();
  const cutoff = now - 24 * 3600 * 1000;
  const buckets: Record<string, { completed: number; failed: number }> = {};

  // Pre-fill the last 24 hours so empty hours show as zero
  for (let i = 23; i >= 0; i--) {
    const hourTs = now - i * 3600 * 1000;
    const key = toHourBucket(hourTs);
    buckets[key] = { completed: 0, failed: 0 };
  }

  for (const ev of events) {
    const ts = new Date(ev.timestamp).getTime();
    if (isNaN(ts) || ts < cutoff) continue;
    const key = toHourBucket(ts);
    if (!buckets[key]) buckets[key] = { completed: 0, failed: 0 };
    if (ev.type === "task_completed" || ev.type === "subtask_completed") buckets[key].completed++;
    if (ev.type === "subtask_failed" || ev.type === "task_failed") buckets[key].failed++;
  }

  return Object.entries(buckets)
    .sort(([a], [b]) => a.localeCompare(b))
    .slice(-24)
    .map(([key, counts]) => ({ name: formatHourLabel(key), ...counts }));
}

/** Read a CSS variable value at runtime. */
function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function TaskTrendChart({ tasks, eventLog, stale }: TaskTrendChartProps) {
  const trendData = useMemo(() => bucketByHour(eventLog), [eventLog]);

  // Current status summary from tasks
  const statusCounts = tasks.available ? tasks.status_counts : {};
  const total = tasks.available ? tasks.total : 0;

  // ponytail: read CSS vars once per render via useMemo so getComputedStyle
  // is called in a single batch instead of 6 separate calls.
  const themeColors = useMemo(() => ({
    gridStroke: cssVar("--border-color") || "#334155",
    axisStroke: cssVar("--text-muted") || "#94a3b8",
    tooltipBg: cssVar("--bg-surface") || "#1e293b",
    tooltipBorder: cssVar("--border-color") || "#334155",
    completedColor: cssVar("--status-completed") || "#22c55e",
    failedColor: cssVar("--status-failed") || "#ef4444",
  }), []);

  return (
    <div className={cn("rounded-lg border border-[var(--border-color)] bg-[var(--bg-surface)] p-4 relative", stale && "opacity-70")}>
      {stale && (
        <div className="stale-badge absolute top-2 left-2 text-[10px] px-1.5 py-0.5 rounded font-medium z-10">
          STALE
        </div>
      )}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-[var(--text-primary)] uppercase tracking-wide">
          Task Activity
        </h2>
        {tasks.available && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-[var(--text-muted)]">{total} total</span>
            {Object.entries(statusCounts).map(([status, count]) => (
              <span
                key={status}
                className={cn(
                  "text-xs px-1.5 py-0.5 rounded",
                  status === "completed" ? "status-completed"
                  : status === "failed" ? "status-failed"
                  : status === "in_progress" ? "status-in_progress"
                  : "status-default"
                )}
              >
                {status}: {count}
              </span>
            ))}
          </div>
        )}
      </div>

      {trendData.length > 0 ? (
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={trendData}>
            <CartesianGrid strokeDasharray="3 3" stroke={themeColors.gridStroke} />
            <XAxis dataKey="name" stroke={themeColors.axisStroke} fontSize={10} interval="preserveStartEnd" />
            <YAxis stroke={themeColors.axisStroke} fontSize={11} />
            <Tooltip
              contentStyle={{ backgroundColor: themeColors.tooltipBg, border: `1px solid ${themeColors.tooltipBorder}`, borderRadius: 4 }}
              labelStyle={{ color: themeColors.axisStroke }}
            />
            <Legend
              wrapperStyle={{ fontSize: 11, color: themeColors.axisStroke }}
              formatter={(value: string) => (
                <span style={{ color: value === "completed" ? themeColors.completedColor : themeColors.failedColor }}>{value}</span>
              )}
            />
            <Bar dataKey="completed" fill={themeColors.completedColor} radius={[2, 2, 0, 0]} />
            <Bar dataKey="failed" fill={themeColors.failedColor} radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <div className="h-[200px] flex items-center justify-center">
          <p className="text-xs text-[var(--text-muted)]">
            {tasks.available ? "No recent task events" : "Task data unavailable"}
          </p>
        </div>
      )}
    </div>
  );
}
