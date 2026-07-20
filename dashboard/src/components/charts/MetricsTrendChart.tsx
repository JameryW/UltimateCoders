import { memo, useState, useCallback, useMemo } from "react";
import type { MetricsSample } from "@/types/dashboard";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";

interface MetricsTrendChartProps {
  trend: MetricsSample[];
  stale?: boolean;
}

/** Each line to plot. */
interface TrendLine {
  key: keyof MetricsSample;
  label: string;
  color: string;
  /** Transform raw value for display (e.g. ms→s). */
  transform?: (v: number) => number;
  /** Format for tooltip. */
  format: (v: number) => string;
}

const LINES: TrendLine[] = [
  { key: "events_per_minute", label: "Events/min", color: "#22c55e", format: (v) => v.toFixed(1) },
  { key: "avg_duration_ms", label: "Avg Duration", color: "#3b82f6", transform: (v) => v / 1000, format: (v) => v < 1 ? `${Math.round(v * 1000)}ms` : `${v.toFixed(1)}s` },
  { key: "error_rate", label: "Error Rate", color: "#ef4444", transform: (v) => v * 100, format: (v) => `${v.toFixed(1)}%` },
  { key: "cluster_utilization", label: "Cluster Util", color: "#f59e0b", transform: (v) => v * 100, format: (v) => `${v.toFixed(0)}%` },
];

const CHART_H = 180;
const PADDING = { top: 12, right: 12, bottom: 32, left: 48 };

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
}

export const MetricsTrendChart = memo(function MetricsTrendChart({ trend, stale }: MetricsTrendChartProps) {
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);
  const [visibleKeys, setVisibleKeys] = useState<Set<string>>(new Set(LINES.map((l) => l.key)));

  const toggleLine = useCallback((key: string) => {
    setVisibleKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) { next.delete(key); } else { next.add(key); }
      return next;
    });
  }, []);

  const activeLines = LINES.filter((l) => visibleKeys.has(l.key));

  // Compute per-line data with transform applied
  const lineData = useMemo(() => {
    return activeLines.map((line) => {
      const values = trend.map((s) => {
        const raw = s[line.key] as number;
        return line.transform ? line.transform(raw) : raw;
      });
      const max = Math.max(...values, 0);
      return { line, values, max };
    });
  }, [trend, activeLines]);

  const globalMax = useMemo(() => Math.max(1, ...lineData.map((d) => d.max)), [lineData]);

  const yTicks = useMemo(() => {
    const step = globalMax <= 4 ? 1 : Math.ceil(globalMax / 4);
    const ticks: number[] = [];
    for (let v = 0; v <= globalMax; v += step) ticks.push(v);
    return ticks;
  }, [globalMax]);

  if (trend.length < 2) {
    return (
      <Card className={cn(stale && "opacity-70")}>
        <CardHeader><CardTitle>Metrics Trend</CardTitle></CardHeader>
        <EmptyState icon="activity" title="Waiting for trend data…" />
      </Card>
    );
  }

  const plotW = Math.max(200, trend.length * 8);
  const svgW = plotW + PADDING.left + PADDING.right;
  const svgH = CHART_H + PADDING.top + PADDING.bottom;
  const plotH = CHART_H;

  const labelEvery = trend.length > 48 ? 10 : trend.length > 24 ? 5 : 3;

  return (
    <Card className={cn(stale && "opacity-70")}>
      <CardHeader>
        <CardTitle>Metrics Trend</CardTitle>
        <div className="flex items-center gap-2">
          {LINES.map((l) => (
            <button
              key={l.key}
              onClick={() => toggleLine(l.key)}
              className={cn(
                "text-xs px-2 py-0.5 rounded transition-colors cursor-pointer",
                visibleKeys.has(l.key) ? "opacity-100" : "opacity-30"
              )}
              style={{ color: l.color, border: `1px solid ${l.color}` }}
            >
              {l.label}
            </button>
          ))}
        </div>
      </CardHeader>

      <div className="relative">
        <svg width="100%" height={svgH} viewBox={`0 0 ${svgW} ${svgH}`} preserveAspectRatio="xMidYMid meet" className="overflow-visible">
          {/* Y grid */}
          {yTicks.map((v) => {
            const y = PADDING.top + plotH - (v / globalMax) * plotH;
            return (
              <g key={v}>
                <line x1={PADDING.left} y1={y} x2={PADDING.left + plotW} y2={y} stroke="var(--border-color)" strokeDasharray="3 3" />
                <text x={PADDING.left - 4} y={y + 3} textAnchor="end" fontSize={10} fill="var(--text-muted)">{v}</text>
              </g>
            );
          })}

          {/* Lines */}
          {lineData.map(({ line, values }) => {
            const points = values.map((v, i) => {
              const x = PADDING.left + (i / (values.length - 1)) * plotW;
              const y = PADDING.top + plotH - (v / globalMax) * plotH;
              return `${x.toFixed(1)},${y.toFixed(1)}`;
            }).join(" ");

            return (
              <polyline
                key={line.key}
                points={points}
                fill="none"
                stroke={line.color}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
                opacity={0.85}
              />
            );
          })}

          {/* X labels */}
          {trend.map((s, i) => {
            if (i % labelEvery !== 0) return null;
            const x = PADDING.left + (i / (trend.length - 1)) * plotW;
            return (
              <text key={i} x={x} y={PADDING.top + plotH + 16} textAnchor="middle" fontSize={9} fill="var(--text-muted)">
                {formatTime(s.timestamp)}
              </text>
            );
          })}

          {/* Hover column */}
          {hoveredIdx !== null && (() => {
            const x = PADDING.left + (hoveredIdx / (trend.length - 1)) * plotW;
            return <line x1={x} y1={PADDING.top} x2={x} y2={PADDING.top + plotH} stroke="var(--text-muted)" strokeDasharray="2 2" />;
          })()}
        </svg>

        {/* Tooltip */}
        {hoveredIdx !== null && trend[hoveredIdx] && (
          <div
            className="absolute top-2 pointer-events-none bg-[var(--bg-surface)] border border-[var(--border-color)] rounded-lg px-3 py-2 shadow-lg text-xs z-10"
            style={{ left: Math.min(PADDING.left + (hoveredIdx / (trend.length - 1)) * plotW, svgW - 160) }}
          >
            <p className="font-medium text-[var(--text-primary)]">{formatTime(trend[hoveredIdx].timestamp)}</p>
            {activeLines.map((line) => {
              const raw = trend[hoveredIdx]![line.key] as number;
              return (
                <p key={line.key} style={{ color: line.color }}>
                  {line.label}: {line.format(raw)}
                </p>
              );
            })}
          </div>
        )}

        {/* Invisible hover zones */}
        <div className="absolute inset-0" style={{ top: PADDING.top, height: plotH }}>
          {trend.map((_, i) => {
            const left = PADDING.left + (i / (trend.length - 1)) * plotW - plotW / trend.length / 2;
            const w = plotW / trend.length;
            return (
              <div
                key={i}
                className="absolute top-0 h-full"
                style={{ left, width: w }}
                onMouseEnter={() => setHoveredIdx(i)}
                onMouseLeave={() => setHoveredIdx(null)}
              />
            );
          })}
        </div>
      </div>
    </Card>
  );
});
