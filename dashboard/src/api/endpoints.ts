import type {
  HealthData,
  WorkersData,
  TasksData,
  SchedulerData,
  CircuitBreakerData,
  EventsData,
  ActionResponse,
  TaskSubmitResponse,
} from "@/types/dashboard";

const BASE = "/dashboard/api";

/** Read the auth token from localStorage (if any). */
function getAuthToken(): string | null {
  try {
    return localStorage.getItem("uc_dashboard_token");
  } catch {
    return null;
  }
}

/** Build headers with optional Authorization Bearer token. */
function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...extra };
  const token = getAuthToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  return headers;
}

async function throwApiError(res: Response): Promise<never> {
  let detail = "";
  try {
    const text = await res.text();
    // Try to extract a JSON error message
    const json = JSON.parse(text);
    detail = json.error ?? json.message ?? json.detail ?? text;
  } catch {
    // Response was not JSON (e.g. HTML from 502/503)
    detail = res.statusText || `HTTP ${res.status}`;
  }
  throw new Error(`API error ${res.status}: ${detail}`);
}

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) await throwApiError(res);
  return res.json() as Promise<T>;
}

async function postJson<T>(url: string, body?: unknown): Promise<T> {
  const headers = authHeaders(
    body ? { "Content-Type": "application/json" } : undefined,
  );
  const res = await fetch(url, {
    method: "POST",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) await throwApiError(res);
  return res.json() as Promise<T>;
}

// ── GET endpoints ───────────────────────────────────────

export function getHealth() {
  return fetchJson<HealthData>(`${BASE}/health`);
}

export function getWorkers() {
  return fetchJson<WorkersData>(`${BASE}/workers`);
}

export function getTasks() {
  return fetchJson<TasksData>(`${BASE}/tasks`);
}

export function getScheduler() {
  return fetchJson<SchedulerData>(`${BASE}/scheduler`);
}

export function getCircuitBreaker() {
  return fetchJson<CircuitBreakerData>(`${BASE}/circuit-breaker`);
}

export function getEvents(taskId?: string, limit = 100) {
  const params = new URLSearchParams();
  if (taskId) params.set("task_id", taskId);
  params.set("limit", String(limit));
  return fetchJson<EventsData>(`${BASE}/events?${params}`);
}

// ── POST endpoints ──────────────────────────────────────

export function submitTask(description: string, projectId?: string) {
  return postJson<TaskSubmitResponse>(`${BASE}/tasks/submit`, {
    description,
    project_id: projectId || "",
  });
}

export function pauseTask(taskId: string) {
  return postJson<ActionResponse>(`${BASE}/tasks/${taskId}/pause`);
}

export function resumeTask(taskId: string) {
  return postJson<ActionResponse>(`${BASE}/tasks/${taskId}/resume`);
}

export function resetCircuitBreaker() {
  return postJson<ActionResponse>(`${BASE}/circuit-breaker/reset`);
}

export function triggerJob(jobId: string) {
  return postJson<ActionResponse>(`${BASE}/scheduler/jobs/${jobId}/trigger`);
}

export function flushPending() {
  return postJson<ActionResponse>(`${BASE}/tasks/flush-pending`);
}
