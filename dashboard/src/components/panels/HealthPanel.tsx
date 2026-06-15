import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn, formatUptime } from "@/lib/utils";
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
      return "text-gray-500";
  }
}

export function HealthPanel({ data }: { data: HealthData }) {
  if (!data.available) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Engine Health</CardTitle>
          <Badge variant="unavailable">unavailable</Badge>
        </CardHeader>
        <p className="text-sm text-gray-500">Engine not available</p>
        {data.error && <p className="text-xs text-red-400 mt-1">{data.error}</p>}
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Engine Health</CardTitle>
        <Badge variant={statusVariant(data.status)}>{data.status}</Badge>
      </CardHeader>

      <ul className="space-y-1.5 mb-3">
        {data.components.map((comp) => (
          <li key={comp.name} className="flex items-center justify-between text-sm">
            <span className="text-gray-400">{comp.name}</span>
            <span className="flex items-center gap-2">
              {comp.details && (
                <span className="text-xs text-gray-500">{comp.details}</span>
              )}
              <span className={cn("text-xs font-medium", componentStatusColor(comp.status))}>
                {comp.status}
              </span>
            </span>
          </li>
        ))}
      </ul>

      <div className="flex items-center justify-between text-xs text-gray-500 border-t border-dark-700 pt-2">
        {data.version && <span>v{data.version}</span>}
        {data.uptime_seconds != null && (
          <span>uptime {formatUptime(data.uptime_seconds)}</span>
        )}
      </div>
    </Card>
  );
}
