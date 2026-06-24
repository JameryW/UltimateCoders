import { memo, useMemo } from "react";
import type { WorkersData, CircuitBreakerData, DashboardEvent } from "@/types/dashboard";
import { cn } from "@/lib/utils";

interface AlertBarProps {
  workers: WorkersData;
  circuitBreaker: CircuitBreakerData;
  eventLog: DashboardEvent[];
  onJumpWorkers?: () => void;
  onJumpCB?: () => void;
}

const ONE_HOUR_MS = 3600_000;

export const AlertBar = memo(function AlertBar({ workers, circuitBreaker, eventLog, onJumpWorkers, onJumpCB }: AlertBarProps) {
  const alerts = useMemo(() => {
    const items: { icon: string; message: string; color: string; onClick?: () => void }[] = [];

    // Stale workers
    if (workers.available) {
      const staleCount = workers.workers.filter((w) => w.heartbeat_stale).length;
      if (staleCount > 0) {
        items.push({
          icon: "⚠",
          message: `${staleCount} stale worker${staleCount > 1 ? "s" : ""}`,
          color: "text-yellow-400",
          onClick: onJumpWorkers,
        });
      }
    }

    // Circuit breaker open
    if (circuitBreaker.available && circuitBreaker.circuit_breaker.state === "open") {
      items.push({
        icon: "🔴",
        message: "Circuit breaker OPEN",
        color: "text-red-400",
        onClick: onJumpCB,
      });
    }

    // Rate limiter high usage
    if (circuitBreaker.available && circuitBreaker.rate_limiter.available) {
      const usedPct = Math.round((1 - circuitBreaker.rate_limiter.remaining_ratio) * 100);
      if (usedPct >= 80) {
        items.push({
          icon: "⚡",
          message: `Rate limiter ${usedPct}% used`,
          color: "text-yellow-400",
          onClick: onJumpCB,
        });
      }
    }

    // Recent failures (last 1h)
    const now = Date.now();
    const recentFailures = eventLog.filter(
      (e) =>
        (e.type === "subtask_failed" || e.type === "task_failed") &&
        new Date(e.timestamp).getTime() >= now - ONE_HOUR_MS
    ).length;
    if (recentFailures > 0) {
      items.push({
        icon: "❌",
        message: `${recentFailures} failure${recentFailures > 1 ? "s" : ""} (1h)`,
        color: "text-red-400",
      });
    }

    return items;
  }, [workers, circuitBreaker, eventLog, onJumpWorkers, onJumpCB]);

  if (alerts.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-3 px-4 py-2 rounded-lg bg-yellow-500/10 border border-yellow-500/30">
      {alerts.map((a, i) => (
        <button
          key={i}
          onClick={a.onClick}
          className={cn(
            "flex items-center gap-1.5 text-xs font-medium",
            a.color,
            a.onClick && "cursor-pointer hover:underline",
            !a.onClick && "cursor-default"
          )}
        >
          <span>{a.icon}</span>
          <span>{a.message}</span>
        </button>
      ))}
    </div>
  );
});
