import { memo } from "react";
import { cn } from "@/lib/utils";

/** A tiny inline sparkline rendered as SVG polyline. */
interface SparklineProps {
  /** Data points (y values). */
  data: number[];
  /** Width in px. */
  width?: number;
  /** Height in px. */
  height?: number;
  /** Line color (CSS value). */
  color?: string;
  /** Fill opacity under the line (0 = no fill). */
  fillOpacity?: number;
  /** Extra className. */
  className?: string;
}

export const Sparkline = memo(function Sparkline({
  data,
  width = 80,
  height = 24,
  color = "var(--text-primary)",
  fillOpacity = 0.1,
  className,
}: SparklineProps) {
  if (data.length < 2) {
    // Not enough points — show empty placeholder
    return (
      <svg width={width} height={height} className={cn("shrink-0", className)}>
        <line x1={0} y1={height / 2} x2={width} y2={height / 2} stroke="var(--border-color)" strokeDasharray="2 2" />
      </svg>
    );
  }

  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1; // Avoid divide-by-zero for flat data
  const pad = 2; // Vertical padding

  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * width;
    const y = pad + (1 - (v - min) / range) * (height - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");

  // Fill polygon: same points + close at bottom
  const fillPoints = points +
    ` ${width},${height} 0,${height}`;

  return (
    <svg width={width} height={height} className={cn("shrink-0", className)}>
      {fillOpacity > 0 && (
        <polygon points={fillPoints} fill={color} opacity={fillOpacity} />
      )}
      <polyline
        points={points}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
});
