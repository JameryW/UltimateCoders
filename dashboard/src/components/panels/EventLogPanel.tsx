import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { DashboardEvent } from "@/types/dashboard";

function eventTypeColor(type: string): string {
  if (type.startsWith("task_submitted")) return "text-blue-400";
  if (type.startsWith("task_completed")) return "text-green-400";
  if (type.startsWith("task_failed")) return "text-red-400";
  if (type.startsWith("task_pause")) return "text-yellow-400";
  if (type.startsWith("task_resume")) return "text-green-400";
  if (type.startsWith("subtask_started") || type.startsWith("subtask_assigned")) return "text-blue-300";
  if (type.startsWith("subtask_completed")) return "text-green-300";
  if (type.startsWith("subtask_failed")) return "text-red-300";
  if (type.startsWith("circuit_breaker_reset")) return "text-yellow-400";
  if (type.startsWith("scheduler_trigger")) return "text-green-400";
  return "text-gray-400";
}

function eventTypeBg(type: string): string {
  if (type.startsWith("task_submitted")) return "bg-blue-900/30";
  if (type.startsWith("task_completed")) return "bg-green-900/30";
  if (type.startsWith("task_failed")) return "bg-red-900/30";
  if (type.startsWith("task_pause")) return "bg-yellow-900/30";
  if (type.startsWith("task_resume")) return "bg-green-900/30";
  if (type.startsWith("subtask_started") || type.startsWith("subtask_assigned")) return "bg-blue-900/20";
  if (type.startsWith("subtask_completed")) return "bg-green-900/20";
  if (type.startsWith("subtask_failed")) return "bg-red-900/20";
  if (type.startsWith("circuit_breaker_reset")) return "bg-yellow-900/30";
  if (type.startsWith("scheduler_trigger")) return "bg-green-900/30";
  return "bg-gray-800/50";
}

function formatTimestamp(ts: string): string {
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return ts;
  }
}

function eventSummary(details: Record<string, unknown>): string {
  const keys = Object.keys(details);
  if (keys.length === 0) return "";
  const first = keys[0]!;
  const val = details[first];
  if (typeof val === "string") return `${first}: ${val}`;
  return `${first}: ${JSON.stringify(val)}`;
}

export function EventLogPanel({ events }: { events: DashboardEvent[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Event Log</CardTitle>
        <Badge>{events.length}</Badge>
      </CardHeader>

      {events.length === 0 ? (
        <p className="text-sm text-gray-500">No events</p>
      ) : (
        <ul className="space-y-1 max-h-64 overflow-y-auto" aria-label="Event log" aria-live="polite">
          {events.map((evt, i) => (
            <li
              key={`${evt.timestamp}-${i}`}
              className={cn("flex items-start gap-2 px-2 py-1 rounded text-xs", eventTypeBg(evt.type))}
            >
              <span className="text-gray-500 shrink-0 font-mono">
                {formatTimestamp(evt.timestamp)}
              </span>
              <span className={cn("shrink-0 font-medium", eventTypeColor(evt.type))}>
                {evt.type}
              </span>
              {Object.keys(evt.details).length > 0 && (
                <span className="text-gray-500 truncate">
                  {eventSummary(evt.details)}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
