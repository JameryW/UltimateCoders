import type { DashboardEvent } from "@/types/dashboard";

const EVENTS_ENDPOINT = "/dashboard/api/events?limit=500";

export function latestDashboardEvents(events: DashboardEvent[], limit = 5): DashboardEvent[] {
  return events.slice(0, limit);
}

export class DashboardEventsRequestError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`Dashboard events request failed (${status})`);
    this.name = "DashboardEventsRequestError";
    this.status = status;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Accept the durable REST shape (`data`) and the dashboard shape (`details`). */
export function normaliseRestDashboardEvent(value: unknown): DashboardEvent | null {
  if (!isRecord(value)) return null;
  const type = typeof value.type === "string" ? value.type.trim() : "";
  const timestamp = typeof value.timestamp === "string" ? value.timestamp.trim() : "";
  if (!type || !timestamp) return null;
  const rawDetails = isRecord(value.details)
    ? value.details
    : isRecord(value.data)
      ? value.data
      : {};
  return { timestamp, type, details: rawDetails };
}

export async function fetchDashboardEvents(
  fetchImpl: typeof fetch,
  token: string | null = null,
  signal?: AbortSignal,
): Promise<DashboardEvent[]> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetchImpl(EVENTS_ENDPOINT, { headers, signal });
  if (!response.ok) throw new DashboardEventsRequestError(response.status);
  const payload: unknown = await response.json();
  if (!isRecord(payload) || !Array.isArray(payload.events)) {
    throw new Error("Dashboard events response is malformed");
  }
  return payload.events
    .map(normaliseRestDashboardEvent)
    .filter((event): event is DashboardEvent => event !== null);
}

/**
 * Merge a historical snapshot with live events without double-counting the same
 * task transition when the REST snapshot and the stream overlap.
 */
export function mergeDashboardEvents(
  historical: readonly DashboardEvent[],
  live: readonly DashboardEvent[],
): DashboardEvent[] {
  const merged: DashboardEvent[] = [];
  const seen = new Set<string>();
  for (const event of [...historical, ...live]) {
    const taskId = String(event.details?.task_id ?? "");
    const subtaskId = String(event.details?.subtask_id ?? "");
    const key = `${event.timestamp}|${event.type}|${taskId}|${subtaskId}`;
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(event);
  }
  return merged;
}
