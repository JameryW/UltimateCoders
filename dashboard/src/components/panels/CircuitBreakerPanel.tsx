import { memo } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn, formatNumber } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import type { CircuitBreakerData } from "@/types/dashboard";

function cbStateVariant(state: string): "closed" | "open" | "half_open" {
  switch (state) {
    case "closed":
      return "closed";
    case "open":
      return "open";
    case "half_open":
      return "half_open";
    default:
      return "open";
  }
}

interface CircuitBreakerPanelProps {
  data: CircuitBreakerData;
  onReset?: () => void;
  stale?: boolean;
  embedded?: boolean;
}

export const CircuitBreakerPanel = memo(function CircuitBreakerPanel({ data, onReset, stale, embedded }: CircuitBreakerPanelProps) {
  const cb = data.circuit_breaker;
  const rl = data.rate_limiter;

  if (!data.available) {
    const unavailable = <EmptyState icon="shield" title="Circuit Breaker not available" description="The circuit breaker endpoint is unreachable" />;
    if (embedded) return unavailable;
    return (
      <Card stale={stale}>
        <CardHeader>
          <CardTitle>Circuit Breaker</CardTitle>
          <Badge variant="unavailable">unavailable</Badge>
        </CardHeader>
        {unavailable}
      </Card>
    );
  }

  const showReset = cb.state === "open" || cb.state === "half_open";

  const content = (
    <>
      {cb.available ? (
        <div className="space-y-1.5 mb-3">
          <div className="flex items-center justify-between text-sm">
            <span className="text-[var(--text-secondary)]">State</span>
            <Badge variant={cbStateVariant(cb.state)}>{cb.state}</Badge>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-[var(--text-secondary)]">Failures</span>
            <span className="text-red-400 font-mono">{cb.failure_count}</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-[var(--text-secondary)]">Total Calls</span>
            <span className="text-[var(--text-primary)] font-mono">{cb.total_calls}</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-[var(--text-secondary)]">Rejected</span>
            <span className="text-yellow-400 font-mono">{cb.total_rejected}</span>
          </div>
        </div>
      ) : (
        <p className="text-sm text-[var(--text-muted)] mb-3">Circuit breaker info unavailable</p>
      )}

      <div className="border-t border-[var(--border-color)] pt-3">
        <p className="text-xs text-[var(--text-muted)] font-medium mb-2">Rate Limiter</p>

        {rl.available ? (
          <div className="space-y-1.5">
            {/* ponytail: RPM usage gauge */}
            <div className="flex items-center justify-between text-sm">
              <span className="text-[var(--text-secondary)]">RPM</span>
              <div className="flex items-center gap-2">
                <div className="w-20 h-1.5 bg-[var(--bg-surface-alt)] rounded overflow-hidden">
                  <div className={cn("h-full rounded", rl.rpm_available / Math.max(rl.total_requests, 1) > 0.8 ? "bg-red-500" : "bg-blue-500")} style={{ width: `${Math.min(100, (rl.rpm_available / Math.max(rl.total_requests, 1)) * 100)}%` }} />
                </div>
                <span className="text-[var(--text-primary)] font-mono text-xs">{formatNumber(rl.rpm_available)}/{formatNumber(rl.total_requests)}</span>
              </div>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-[var(--text-secondary)]">TPM</span>
              <span className="text-[var(--text-primary)] font-mono">{formatNumber(rl.tpm_available)}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-[var(--text-secondary)]">Active</span>
              <span className="text-blue-400 font-mono">{rl.active_count}</span>
            </div>
          </div>
        ) : (
          <p className="text-sm text-[var(--text-muted)]">Rate limiter info unavailable</p>
        )}
      </div>
    </>
  );

  if (embedded) return content;

  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Circuit Breaker</CardTitle>
        {showReset && onReset && (
          <button
            onClick={onReset}
            aria-label="Reset circuit breaker to closed state"
            className="btn-action-error px-2 py-0.5 rounded text-xs cursor-pointer"
          >
            Reset
          </button>
        )}
      </CardHeader>
      {content}
    </Card>
  );
});
