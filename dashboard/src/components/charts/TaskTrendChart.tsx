import { useMemo, useState, useCallback } from "react";
import type { TasksData, DashboardEvent } from "@/types/dashboard";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";

interface TaskTrendChartProps {
  tasks: TasksData;
  eventLog: DashboardEvent[];
  stale?: boolean;
}

type TimeRange = "1h" | "6h" | "24h" | "7d";

const TIME_RANGE_HOURS: Record<TimeRange, number> = { "1h": 1, "6h": 6, "24h": 24, "7d": 168 };

function toBucketKey(ts: number, range: TimeRange): string {
  const d = new Date(ts);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  if (range === "7d") return `${yyyy}-${mm}-${dd}`;
  return `${yyyy}-${mm}-${dd}T${hh}:00`;
}

function formatBucketLabel(key: string, range: TimeRange): string {
  const d = new Date(key);
  if (range === "7d") return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
    d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
}

function bucketEvents(events: DashboardEvent[], range: TimeRange): { name: string; submitted: number; completed: number; failed: number }[] {
  const hours = TIME_RANGE_HOURS[range];
  const now = Date.now();
  const cutoff = now - hours * 3600 * 1000;
  const buckets: Record<string, { submitted: number; completed: number; failed: number }> = {};
  const bucketMs = range === "7d" ? 24 * 3600 * 1000 : 3600 * 1000;
  // ponytail: 7d = 7 daily buckets, other ranges = hourly buckets matching hours count
  const bucketCount = range === "7d" ? 7 : hours;
  for (let i = bucketCount - 1; i >= 0; i--) {
    const ts = now - i * bucketMs;
    const key = toBucketKey(ts, range);
    buckets[key] = { submitted: 0, completed: 0, failed: 0 };
  }
  for (const ev of events) {
    const ts = new Date(ev.timestamp).getTime();
    if (isNaN(ts) || ts < cutoff) continue;
    const key = toBucketKey(ts, range);
    if (!buckets[key]) buckets[key] = { submitted: 0, completed: 0, failed: 0 };
    if (ev.type === "task_submitted" || ev.type === "subtask_assigned") buckets[key].submitted++;
    if (ev.type === "task_completed" || ev.type === "subtask_completed") buckets[key].completed++;
    if (ev.type === "subtask_failed" || ev.type === "task_failed") buckets[key].failed++;
  }
  return Object.entries(buckets)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, counts]) => ({ name: formatBucketLabel(key, range), ...counts }));
}

const BAR_WIDTH = 14;
const BAR_GAP = 4;
const CHART_H = 160;
const PADDING = { top: 8, right: 8, bottom: 28, left: 28 };

export function TaskTrendChart({ tasks, eventLog, stale }: TaskTrendChartProps) {
  const [range, setRange] = useState<TimeRange>("24h");
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);
  const trendData = useMemo(() => bucketEvents(eventLog, range), [eventLog, range]);
  const statusCounts = tasks.available ? tasks.status_counts : {};
  const total = tasks.available ? tasks.total : 0;
  const maxVal = useMemo(() => Math.max(1, ...trendData.map((d) => d.submitted + d.completed + d.failed)), [trendData]);
  const plotW = trendData.length * (BAR_WIDTH + BAR_GAP) - BAR_GAP;
  const svgW = plotW + PADDING.left + PADDING.right;
  const svgH = CHART_H + PADDING.top + PADDING.bottom;
  const plotH = CHART_H;
  const yTicks = useMemo(() => {
    const step = maxVal <= 4 ? 1 : Math.ceil(maxVal / 4);
    const ticks: number[] = [];
    for (let v = 0; v <= maxVal; v += step) ticks.push(v);
    return ticks;
  }, [maxVal]);
  const labelEvery = trendData.length > 48 ? 12 : trendData.length > 24 ? 6 : 4;
  const handleBarHover = useCallback((i: number | null) => setHoveredIdx(i), []);

  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Task Activity</CardTitle>
        <div className="flex items-center gap-2">
          <div className="flex items-center bg-[var(--bg-primary)] rounded-md border border-[var(--border-color)] overflow-hidden">
            {(["1h", "6h", "24h", "7d"] as TimeRange[]).map((r) => (
              <button
                key={r}
                onClick={() => setRange(r)}
                className={cn(
                  "text-xs px-2 py-0.5 transition-colors cursor-pointer",
                  range === r ? "bg-blue-600 text-white" : "text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                )}
              >
                {r}
              </button>
            ))}
          </div>
          {tasks.available && (
            <>
              <span className="text-xs text-[var(--text-muted)]">{total} total</span>
              {Object.entries(statusCounts).map(([status, count]) => (
                <span key={status} className={cn("text-xs px-1.5 py-0.5 rounded", status === "completed" ? "status-completed" : status === "failed" ? "status-failed" : status === "in_progress" ? "status-in_progress" : "status-default")}>
                  {status}: {count}
                </span>
              ))}
            </>
          )}
        </div>
      </CardHeader>

      {trendData.length > 0 ? (
        <div className="relative">
          <svg width="100%" height={svgH} viewBox={`0 0 ${svgW} ${svgH}`} preserveAspectRatio="xMidYMid meet" className="overflow-visible">
            {yTicks.map((v) => {
              const y = PADDING.top + plotH - (v / maxVal) * plotH;
              return (
                <g key={v}>
                  <line x1={PADDING.left} y1={y} x2={PADDING.left + plotW} y2={y} stroke="var(--border-color)" strokeDasharray="3 3" />
                  <text x={PADDING.left - 4} y={y + 3} textAnchor="end" fontSize={10} fill="var(--text-muted)">{v}</text>
                </g>
              );
            })}
            {trendData.map((d, i) => {
              const x = PADDING.left + i * (BAR_WIDTH + BAR_GAP);
              const submittedH = (d.submitted / maxVal) * plotH;
              const completedH = (d.completed / maxVal) * plotH;
              const failedH = (d.failed / maxVal) * plotH;
              // Stacked: submitted (bottom) → completed → failed (top)
              const submittedY = PADDING.top + plotH - submittedH;
              const completedY = submittedY - completedH;
              const failedY = completedY - failedH;
              const isHovered = hoveredIdx === i;
              return (
                <g key={d.name} onMouseEnter={() => handleBarHover(i)} onMouseLeave={() => handleBarHover(null)}>
                  <rect x={x - BAR_GAP / 2} y={PADDING.top} width={BAR_WIDTH + BAR_GAP} height={plotH} fill={isHovered ? "var(--bg-surface-alt)" : "transparent"} opacity={0.5} />
                  {d.submitted > 0 && <rect x={x} y={submittedY} width={BAR_WIDTH} height={submittedH} fill="var(--status-submitted, #3b82f6)" rx={1} opacity={isHovered ? 1 : 0.85} />}
                  {d.completed > 0 && <rect x={x} y={completedY} width={BAR_WIDTH} height={completedH} fill="var(--status-completed, #22c55e)" rx={1} opacity={isHovered ? 1 : 0.85} />}
                  {d.failed > 0 && <rect x={x} y={failedY} width={BAR_WIDTH} height={failedH} fill="var(--status-failed, #ef4444)" rx={1} opacity={isHovered ? 1 : 0.85} />}
                  {i % labelEvery === 0 && <text x={x + BAR_WIDTH / 2} y={PADDING.top + plotH + 16} textAnchor="middle" fontSize={9} fill="var(--text-muted)">{d.name.split(" ")[1] ?? d.name}</text>}
                </g>
              );
            })}
            <rect x={PADDING.left} y={svgH - 6} width={8} height={8} fill="var(--status-submitted, #3b82f6)" rx={1} />
            <text x={PADDING.left + 12} y={svgH - 0} fontSize={10} fill="var(--text-muted)">submitted</text>
            <rect x={PADDING.left + 68} y={svgH - 6} width={8} height={8} fill="var(--status-completed, #22c55e)" rx={1} />
            <text x={PADDING.left + 80} y={svgH - 0} fontSize={10} fill="var(--text-muted)">completed</text>
            <rect x={PADDING.left + 140} y={svgH - 6} width={8} height={8} fill="var(--status-failed, #ef4444)" rx={1} />
            <text x={PADDING.left + 152} y={svgH - 0} fontSize={10} fill="var(--text-muted)">failed</text>
          </svg>
          {hoveredIdx !== null && trendData[hoveredIdx] && (
            <div className="absolute top-2 pointer-events-none bg-[var(--bg-surface)] border border-[var(--border-color)] rounded-lg px-3 py-2 shadow-lg text-xs z-10" style={{ left: Math.min(PADDING.left + hoveredIdx * (BAR_WIDTH + BAR_GAP) + BAR_WIDTH / 2, svgW - 120) }}>
              <p className="font-medium text-[var(--text-primary)]">{trendData[hoveredIdx].name}</p>
              <p className="text-blue-400">submitted: {trendData[hoveredIdx].submitted}</p>
              <p className="text-green-400">completed: {trendData[hoveredIdx].completed}</p>
              <p className="text-red-400">failed: {trendData[hoveredIdx].failed}</p>
            </div>
          )}
        </div>
      ) : (
        <EmptyState icon="activity" title={tasks.available ? "No recent task events" : "Task data unavailable"} description={tasks.available ? "Activity will appear here as tasks complete" : "Connect to the engine to see task activity"} />
      )}
    </Card>
  );
}
