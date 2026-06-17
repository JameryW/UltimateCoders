import { useMemo } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
} from "recharts";
import type { TasksData, DashboardEvent } from "@/types/dashboard";

interface TaskTrendChartProps {
  tasks: TasksData;
  eventLog: DashboardEvent[];
}

/** Bucket events by hour (last 24h) and count completions vs failures. */
function bucketByHour(events: DashboardEvent[]): { name: string; completed: number; failed: number }[] {
  const now = Date.now();
  const buckets: Record<string, { completed: number; failed: number }> = {};

  for (const ev of events) {
    const ts = new Date(ev.timestamp).getTime();
    if (isNaN(ts) || now - ts > 24 * 3600 * 1000) continue;
    const bucket = new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (!buckets[bucket]) buckets[bucket] = { completed: 0, failed: 0 };
    if (ev.type === "task_completed" || ev.type === "subtask_completed") buckets[bucket].completed++;
    if (ev.type === "subtask_failed" || ev.type === "task_failed") buckets[bucket].failed++;
  }

  return Object.entries(buckets)
    .slice(-12) // show last 12 buckets
    .map(([name, counts]) => ({ name, ...counts }));
}

export function TaskTrendChart({ tasks, eventLog }: TaskTrendChartProps) {
  const trendData = useMemo(() => bucketByHour(eventLog), [eventLog]);

  // Current status summary from tasks
  const statusCounts = tasks.available ? tasks.status_counts : {};
  const total = tasks.available ? tasks.total : 0;

  return (
    <div className="rounded-lg border border-dark-700 bg-dark-800 p-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Task Activity
        </h2>
        {tasks.available && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-500">{total} total</span>
            {Object.entries(statusCounts).map(([status, count]) => (
              <span
                key={status}
                className={`text-xs px-1.5 py-0.5 rounded ${
                  status === "completed" ? "bg-green-900/50 text-green-300"
                  : status === "failed" ? "bg-red-900/50 text-red-300"
                  : status === "in_progress" ? "bg-blue-900/50 text-blue-300"
                  : "bg-gray-800 text-gray-400"
                }`}
              >
                {status}: {count}
              </span>
            ))}
          </div>
        )}
      </div>

      {trendData.length > 0 ? (
        <ResponsiveContainer width="100%" height={180}>
          <BarChart data={trendData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
            <XAxis dataKey="name" stroke="#94a3b8" fontSize={10} />
            <YAxis stroke="#94a3b8" fontSize={11} />
            <Tooltip
              contentStyle={{ backgroundColor: "#1e293b", border: "1px solid #334155", borderRadius: 4 }}
              labelStyle={{ color: "#94a3b8" }}
            />
            <Bar dataKey="completed" fill="#22c55e" radius={[2, 2, 0, 0]} />
            <Bar dataKey="failed" fill="#ef4444" radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <div className="h-[180px] flex items-center justify-center">
          <p className="text-xs text-gray-500">
            {tasks.available ? "No recent task events" : "Task data unavailable"}
          </p>
        </div>
      )}
    </div>
  );
}
