import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { formatNumber } from "@/lib/utils";
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
}

export function CircuitBreakerPanel({ data, onReset, stale }: CircuitBreakerPanelProps) {
  const cb = data.circuit_breaker;
  const rl = data.rate_limiter;

  if (!data.available) {
    return (
      <Card stale={stale}>
        <CardHeader>
          <CardTitle>Circuit Breaker</CardTitle>
          <Badge variant="unavailable">unavailable</Badge>
        </CardHeader>
        <p className="text-sm text-gray-500">Circuit Breaker not available</p>
      </Card>
    );
  }

  const showReset = cb.state === "open" || cb.state === "half_open";

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

      {cb.available ? (
        <div className="space-y-1.5 mb-3">
          <div className="flex items-center justify-between text-sm">
            <span className="text-gray-400">State</span>
            <Badge variant={cbStateVariant(cb.state)}>{cb.state}</Badge>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-gray-400">Failures</span>
            <span className="text-red-400 font-mono">{cb.failure_count}</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-gray-400">Total Calls</span>
            <span className="text-[var(--text-primary)] font-mono">{cb.total_calls}</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-gray-400">Rejected</span>
            <span className="text-yellow-400 font-mono">{cb.total_rejected}</span>
          </div>
        </div>
      ) : (
        <p className="text-sm text-gray-500 mb-3">Circuit breaker info unavailable</p>
      )}

      <div className="border-t border-[var(--border-color)] pt-3">
        <p className="text-xs text-gray-500 uppercase tracking-wide mb-2">Rate Limiter</p>

        {rl.available ? (
          <div className="space-y-1.5">
            <div className="flex items-center justify-between text-sm">
              <span className="text-gray-400">RPM</span>
              <span className="text-[var(--text-primary)] font-mono">{formatNumber(rl.rpm_available)}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-gray-400">TPM</span>
              <span className="text-[var(--text-primary)] font-mono">{formatNumber(rl.tpm_available)}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-gray-400">Active</span>
              <span className="text-blue-400 font-mono">{rl.active_count}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-gray-400">Total</span>
              <span className="text-[var(--text-primary)] font-mono">{rl.total_requests}</span>
            </div>
          </div>
        ) : (
          <p className="text-sm text-gray-500">Rate limiter info unavailable</p>
        )}
      </div>
    </Card>
  );
}
