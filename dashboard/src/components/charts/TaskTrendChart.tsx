import { useMemo } from "react";
import type { TasksData, DashboardEvent } from "@/types/dashboard";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";

interface TaskTrendChartProps {
  tasks: TasksData;
  eventLog: DashboardEvent[];
  stale?: boolean;
}

/** Truncate a timestamp to the start of its hour. */
function toHourBucket(ts: number): string {
  const d = new Date(ts);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}T${hh}:00`;
}

/** Format an hour-bucket key for display, e.g. "Jun 18 14:00". */
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

const BAR_WIDTH = 14;
const BAR_GAP = 4;
const CHART_H = 160;
const PADDING = { top: 8, right: 8, bottom: 28, left: 28 };

export function TaskTrendChart({ tasks, eventLog, stale }: TaskTrendChartProps) {
  const trendData = useMemo(() => bucketByHour(eventLog), [eventLog]);

  const statusCounts = tasks.available ? tasks.status_counts : {};
  const total = tasks.available ? tasks.total : 0;

  const maxVal = useMemo(() => {
    const m = Math.max(1, ...trendData.map((d) => d.completed + d.failed));
    return m;
  }, [trendData]);

  const plotW = trendData.length * (BAR_WIDTH + BAR_GAP) - BAR_GAP;
  const svgW = plotW + PADDING.left + PADDING.right;
  const svgH = CHART_H + PADDING.top + PADDING.bottom;
  const plotH = CHART_H;

  // Y-axis ticks
  const yTicks = useMemo(() => {
    const step = maxVal <= 4 ? 1 : Math.ceil(maxVal / 4);
    const ticks: number[] = [];
    for (let v = 0; v <= maxVal; v += step) ticks.push(v);
    return ticks;
  }, [maxVal]);

  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Task Activity</CardTitle>
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
      </CardHeader>

      {trendData.length > 0 ? (
        <svg
          width="100%"
          height={svgH}
          viewBox={`0 0 ${svgW} ${svgH}`}
          preserveAspectRatio="xMidYMid meet"
          className="overflow-visible"
        >
          {/* Y-axis */}
          {yTicks.map((v) => {
            const y = PADDING.top + plotH - (v / maxVal) * plotH;
            return (
              <g key={v}>
                <line x1={PADDING.left} y1={y} x2={PADDING.left + plotW} y2={y} stroke="var(--border-color)" strokeDasharray="3 3" />
                <text x={PADDING.left - 4} y={y + 3} textAnchor="end" fontSize={10} fill="var(--text-muted)">{v}</text>
              </g>
            );
          })}

          {/* Bars */}
          {trendData.map((d, i) => {
            const x = PADDING.left + i * (BAR_WIDTH + BAR_GAP);
            const completedH = (d.completed / maxVal) * plotH;
            const failedH = (d.failed / maxVal) * plotH;
            const totalH = completedH + failedH;
            const barY = PADDING.top + plotH - totalH;

            return (
              <g key={d.name}>
                {/* Completed portion (bottom) */}
                {d.completed > 0 && (
                  <rect
                    x={x} y={PADDING.top + plotH - completedH}
                    width={BAR_WIDTH} height={completedH}
                    fill="var(--status-completed, #22c55e)" rx={1}
                  />
                )}
                {/* Failed portion (stacked on top) */}
                {d.failed > 0 && (
                  <rect
                    x={x} y={barY}
                    width={BAR_WIDTH} height={failedH}
                    fill="var(--status-failed, #ef4444)" rx={1}
                  />
                )}
                {/* X-axis label — show every 4th to avoid crowding */}
                {i % 4 === 0 && (
                  <text
                    x={x + BAR_WIDTH / 2}
                    y={PADDING.top + plotH + 16}
                    textAnchor="middle"
                    fontSize={9}
                    fill="var(--text-muted)"
                  >
                    {d.name.split(" ")[1] ?? d.name}
                  </text>
                )}
              </g>
            );
          })}

          {/* Legend */}
          <rect x={PADDING.left} y={svgH - 6} width={8} height={8} fill="var(--status-completed, #22c55e)" rx={1} />
          <text x={PADDING.left + 12} y={svgH - 0} fontSize={10} fill="var(--text-muted)">completed</text>
          <rect x={PADDING.left + 72} y={svgH - 6} width={8} height={8} fill="var(--status-failed, #ef4444)" rx={1} />
          <text x={PADDING.left + 84} y={svgH - 0} fontSize={10} fill="var(--text-muted)">failed</text>
        </svg>
      ) : (
        <EmptyState
          icon="activity"
          title={tasks.available ? "No recent task events" : "Task data unavailable"}
          description={tasks.available ? "Activity will appear here as tasks complete" : "Connect to the engine to see task activity"}
        />
      )}
    </Card>
  );
}
