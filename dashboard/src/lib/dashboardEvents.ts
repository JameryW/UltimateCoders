import type { DashboardEvent } from "@/types/dashboard";

export function latestDashboardEvents(events: DashboardEvent[], limit = 5): DashboardEvent[] {
  return events.slice(0, limit);
}
