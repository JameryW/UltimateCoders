import { memo } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn, formatUptime } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import type { HealthData } from "@/types/dashboard";

function statusVariant(status: string): "ok" | "degraded" | "error" | "unavailable" {
  switch (status) {
    case "ok":
      return "ok";
    case "degraded":
      return "degraded";
    case "error":
      return "error";
    default:
      return "unavailable";
  }
}

function componentStatusColor(status: string): string {
  switch (status) {
    case "ok":
      return "text-green-400";
    case "degraded":
      return "text-yellow-400";
    case "error":
      return "text-red-400";
    default:
      return "text-[var(--text-muted)]";
  }
}

interface HealthPanelProps {
  data: HealthData;
  stale?: boolean;
  embedded?: boolean;
}

export const HealthPanel = memo(function HealthPanel({ data, stale, embedded }: HealthPanelProps) {
  if (!data.available) {
    const unavailable = (
      <EmptyState icon="health" title="Engine not available" description={data.error ?? "The engine health endpoint is unreachable"} />
    );
    if (embedded) return unavailable;
    return (
      <Card stale={stale}>
        <CardHeader>
          <CardTitle>Engine Health</CardTitle>
          <Badge variant="unavailable">unavailable</Badge>
        </CardHeader>
        {unavailable}
      </Card>
    );
  }

  const content = (
    <>
      <ul className="space-y-1.5 mb-3" aria-label="Engine components">
        {data.components.map((comp) => (
          <li key={comp.name} className="flex items-center justify-between text-sm">
            <span className="text-[var(--text-secondary)]">{comp.name}</span>
            <span className="flex items-center gap-2">
              {comp.details && (
                <span className="text-xs text-[var(--text-muted)]">{comp.details}</span>
              )}
              <span className={cn("text-xs font-medium", componentStatusColor(comp.status))}>
                {comp.status}
              </span>
            </span>
          </li>
        ))}
      </ul>

      <div className="flex items-center justify-between text-xs text-[var(--text-muted)] border-t border-[var(--border-color)] pt-2">
        {data.version && <span>v{data.version}</span>}
        {data.uptime_seconds != null && (
          <span>uptime {formatUptime(data.uptime_seconds)}</span>
        )}
      </div>
    </>
  );

  if (embedded) return content;

  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Engine Health</CardTitle>
        <Badge variant={statusVariant(data.status)}>{data.status}</Badge>
      </CardHeader>
      {content}
    </Card>
  );
});
