import { memo, useMemo, useState, useCallback, useEffect, useRef } from "react";
import type { WorkersData, DashboardEvent, MetricsSnapshot, AlertEvent, AlertRecord } from "@/types/dashboard";
import { cn } from "@/lib/utils";
import { getStoredToken } from "@/hooks/useAuth";

interface AlertBarProps {
  workers: WorkersData;
  eventLog: DashboardEvent[];
  metrics: MetricsSnapshot | null;
  activeAlerts: AlertEvent[];
  onJumpWorkers?: () => void;
}

const ONE_HOUR_MS = 3600_000;

export const AlertBar = memo(function AlertBar({
  workers, eventLog, metrics, activeAlerts, onJumpWorkers,
}: AlertBarProps) {
  const [showHistory, setShowHistory] = useState(false);
  const [alertHistory, setAlertHistory] = useState<AlertRecord[]>([]);
  // ponytail: track whether the history fetch failed, so the empty state can
  // distinguish "no alerts recorded" from "history unavailable" instead of
  // showing the same message for both (misleading on a failed fetch).
  const [historyError, setHistoryError] = useState(false);
  const historyFetched = useRef(false);

  const alerts = useMemo(() => {
    const items: { icon: string; message: string; color: string; alertType: string; onClick?: () => void }[] = [];

    // 1. Stale workers
    if (workers.available) {
      const staleCount = workers.workers.filter((w) => w.heartbeat_stale).length;
      if (staleCount > 0) {
        items.push({
          icon: "⚠", message: `${staleCount} stale worker${staleCount > 1 ? "s" : ""}`,
          color: "text-yellow-400", alertType: "stale_workers", onClick: onJumpWorkers,
        });
      }
    }

    // 4. Recent failures (last 1h)
    const now = Date.now();
    const recentFailures = eventLog.filter(
      (e) =>
        (e.type === "subtask_failed" || e.type === "task_failed") &&
        new Date(e.timestamp).getTime() >= now - ONE_HOUR_MS
    ).length;
    if (recentFailures > 0) {
      items.push({
        icon: "❌", message: `${recentFailures} failure${recentFailures > 1 ? "s" : ""} (1h)`,
        color: "text-red-400", alertType: "recent_failures",
      });
    }

    // 5. Error spike (from metrics)
    if (metrics?.event?.error_spike) {
      items.push({
        icon: "🚨", message: "Error rate spike (>30%)",
        color: "text-red-400", alertType: "error_spike",
      });
    }

    // 6. Slow tasks (from metrics)
    if (metrics?.task?.slow_tasks_count && metrics.task.slow_tasks_count > 0) {
      items.push({
        icon: "🐌", message: `${metrics.task.slow_tasks_count} slow task${metrics.task.slow_tasks_count > 1 ? "s" : ""} (>5min)`,
        color: "text-yellow-400", alertType: "slow_tasks",
      });
    }

    // 7. High latency (from metrics)
    if (metrics?.task?.p95_duration_ms && metrics.task.p95_duration_ms > 300000) {
      items.push({
        icon: "⏱", message: `P95 latency ${(metrics.task.p95_duration_ms / 1000).toFixed(0)}s`,
        color: "text-yellow-400", alertType: "high_latency",
      });
    }

    return items;
  }, [workers, eventLog, metrics, onJumpWorkers]);

  // Fetch alert history on first expand
  const fetchHistory = useCallback(async () => {
    try {
      // ponytail: F69 — authenticate (backend gates /dashboard/api/* off localhost).
      const alertsToken = getStoredToken();
      const res = await fetch(
        `/dashboard/api/alerts?limit=100${alertsToken ? `&token=${encodeURIComponent(alertsToken)}` : ""}`,
      );
      if (res.ok) {
        const data = await res.json();
        setAlertHistory(data.alerts || []);
        // ponytail: clear any prior error on success.
        setHistoryError(false);
      } else {
        // ponytail: non-ok response (e.g. endpoint absent in gRPC-only deploy)
        // — mark failed so the empty state names the cause instead of saying
        // "No alerts recorded".
        setHistoryError(true);
      }
    } catch {
      // ponytail: network/transport failure — same misleading-avoidance.
      setHistoryError(true);
    }
  }, []);

  const toggleHistory = useCallback(() => {
    if (!showHistory && !historyFetched.current) {
      fetchHistory();
      historyFetched.current = true;
    }
    setShowHistory(prev => !prev);
  }, [showHistory, fetchHistory]);

  // Refresh history when new alerts arrive
  useEffect(() => {
    if (activeAlerts.length > 0 && historyFetched.current) {
      fetchHistory();
    }
  }, [activeAlerts, fetchHistory]);

  if (alerts.length === 0 && activeAlerts.length === 0) return null;

  return (
    <div className="relative">
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
        <button
          onClick={toggleHistory}
          className="ml-auto text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors"
          title="Alert history"
        >
          {showHistory ? "✕ Close" : "📋 History"}
        </button>
      </div>

      {/* Dropdown history panel */}
      {showHistory && (
        <div className="absolute top-full left-0 right-0 mt-1 z-50 rounded-lg border border-[var(--border-color)] bg-[var(--bg-surface)] shadow-lg max-h-80 overflow-y-auto">
          <div className="px-3 py-2 border-b border-[var(--border-color)]">
            <span className="text-xs font-semibold text-[var(--text-muted)]">Alert History</span>
          </div>
          {alertHistory.length === 0 ? (
            <div className="px-3 py-4 text-xs text-[var(--text-muted)] text-center">
              {historyError ? "Alert history unavailable" : "No alerts recorded"}
            </div>
          ) : (
            <div className="divide-y divide-[var(--border-color)]">
              {alertHistory.map((a) => (
                <div key={a.id} className={cn(
                  "px-3 py-2 flex items-center gap-2 text-xs",
                  a.resolved ? "opacity-50" : "",
                )}>
                  <span className={cn(
                    "w-2 h-2 rounded-full shrink-0",
                    a.severity === "critical" ? "bg-red-400" : "bg-yellow-400",
                    a.resolved && "bg-[var(--text-muted)]",
                  )} />
                  <span className={cn(
                    "flex-1 min-w-0 truncate",
                    a.resolved ? "line-through" : "text-[var(--text-primary)]",
                  )}>
                    {a.message}
                  </span>
                  <span className="text-[var(--text-muted)] shrink-0">
                    {new Date(a.timestamp * 1000).toLocaleTimeString()}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
});
