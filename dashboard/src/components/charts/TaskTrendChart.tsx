/**
 * Task trend chart placeholder — Recharts component.
 * Currently renders a static placeholder. Future: fetch task completion
 * rate, latency distribution, etc. from a dedicated API endpoint.
 */

import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  ResponsiveContainer,
} from "recharts";

// Static placeholder data
const PLACEHOLDER_DATA = [
  { name: "Mon", completed: 4, failed: 1 },
  { name: "Tue", completed: 6, failed: 0 },
  { name: "Wed", completed: 3, failed: 2 },
  { name: "Thu", completed: 8, failed: 1 },
  { name: "Fri", completed: 5, failed: 0 },
];

export function TaskTrendChart() {
  return (
    <div className="rounded-lg border border-dark-700 bg-dark-800 p-4">
      <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-3">
        Task Completion Trend
      </h2>
      <p className="text-xs text-gray-500 mb-2">
        ⚡ Placeholder — real data requires backend aggregation API
      </p>
      <ResponsiveContainer width="100%" height={180}>
        <BarChart data={PLACEHOLDER_DATA}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="name" stroke="#94a3b8" fontSize={11} />
          <YAxis stroke="#94a3b8" fontSize={11} />
          <Bar dataKey="completed" fill="#22c55e" radius={[2, 2, 0, 0]} />
          <Bar dataKey="failed" fill="#ef4444" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
