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
      return "half_open";
    case "half_open":
      return "open";
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

/** Render a key-value row, showing N/A for -1 sentinel values. */
function MetricRow({ label, value, className, naText }: { label: string; value: number; className?: string; naText?: string }) {
  const isNa = value < 0;
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-[var(--text-secondary)]">{label}</span>
      <span className={cn("font-mono", isNa ? "text-[var(--text-muted)]" : className)}>
        {isNa ? (naText ?? "N/A") : formatNumber(value)}
      </span>
    </div>
  );
}

/** Render engine CB/RL metrics as a compact key-value block. */
function EngineMetricsBlock({ label, metrics }: { label: string; metrics: Record<string, unknown> }) {
  const entries = Object.entries(metrics).filter(([, v]) => v !== undefined && v !== null && v !== "");
  if (entries.length === 0) return null;
  return (
    <div className="mt-2">
      <p className="text-xs text-[var(--text-muted)] font-medium mb-1">{label}</p>
      <div className="space-y-0.5">
        {entries.map(([k, v]) => (
          <div key={k} className="flex items-center justify-between text-xs">
            <span className="text-[var(--text-secondary)]">{k.replace(/_/g, " ")}</span>
            <span className="text-[var(--text-primary)] font-mono">{String(v)}</span>
          </div>
        ))}
      </div>
    </div>
  );
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

  // ponytail: RPM gauge uses remainingRatio (0-1) from proto
  const rpmUsedPct = rl.available ? Math.round((1 - rl.remaining_ratio) * 100) : 0;
  const rpmGaugeColor = rpmUsedPct > 80 ? "bg-red-500" : rpmUsedPct > 50 ? "bg-yellow-500" : "bg-blue-500";

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
          <MetricRow label="Threshold" value={cb.failure_threshold} className="text-yellow-400" />
          <MetricRow label="Total Calls" value={cb.total_calls} />
          <MetricRow label="Rejected" value={cb.total_rejected} className="text-yellow-400" />
          {cb.recovery_timeout_seconds > 0 && (
            <div className="flex items-center justify-between text-sm">
              <span className="text-[var(--text-secondary)]">Recovery</span>
              <span className="text-[var(--text-primary)] font-mono">{cb.recovery_timeout_seconds}s</span>
            </div>
          )}
          {cb.last_failure && (
            <div className="flex items-center justify-between text-sm">
              <span className="text-[var(--text-secondary)]">Last Failure</span>
              <span className="text-red-300 font-mono text-xs truncate max-w-[60%]" title={cb.last_failure}>{cb.last_failure}</span>
            </div>
          )}
        </div>
      ) : (
        <p className="text-sm text-[var(--text-muted)] mb-3">Circuit breaker info unavailable</p>
      )}

      <div className="border-t border-[var(--border-color)] pt-3">
        <p className="text-xs text-[var(--text-muted)] font-medium mb-2">Rate Limiter</p>

        {rl.available ? (
          <div className="space-y-1.5">
            {/* RPM usage gauge — driven by remainingRatio */}
            <div className="flex items-center justify-between text-sm">
              <span className="text-[var(--text-secondary)]">RPM</span>
              <div className="flex items-center gap-2">
                <div className="w-20 h-1.5 bg-[var(--bg-surface-alt)] rounded overflow-hidden">
                  <div className={cn("h-full rounded", rpmGaugeColor)} style={{ width: `${rpmUsedPct}%` }} />
                </div>
                <span className="text-[var(--text-primary)] font-mono text-xs">{formatNumber(rl.rpm_available)}/{formatNumber(rl.total_requests)}</span>
              </div>
            </div>
            <MetricRow label="TPM" value={rl.tpm_available} />
            <div className="flex items-center justify-between text-sm">
              <span className="text-[var(--text-secondary)]">Active</span>
              <span className="text-blue-400 font-mono">{rl.active_count}</span>
            </div>
            {rl.window_seconds > 0 && (
              <div className="flex items-center justify-between text-sm">
                <span className="text-[var(--text-secondary)]">Window</span>
                <span className="text-[var(--text-primary)] font-mono">{rl.window_seconds}s</span>
              </div>
            )}
          </div>
        ) : (
          <p className="text-sm text-[var(--text-muted)]">Rate limiter info unavailable</p>
        )}
      </div>

      {/* Engine-side metrics (from Rust gRPC server) */}
      {(Object.keys(data.engine_circuit_breaker).length > 0 || Object.keys(data.engine_rate_limiter).length > 0) && (
        <div className="border-t border-[var(--border-color)] pt-3">
          <p className="text-xs text-[var(--text-muted)] font-medium mb-1">Engine Metrics</p>
          <EngineMetricsBlock label="Circuit Breaker" metrics={data.engine_circuit_breaker} />
          <EngineMetricsBlock label="Rate Limiter" metrics={data.engine_rate_limiter} />
        </div>
      )}
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
