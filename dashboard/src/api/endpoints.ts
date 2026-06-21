/**
 * DEPRECATED: These FastAPI endpoint functions are no longer used.
 * All dashboard data is now fetched via gRPC-Web (DashboardService).
 *
 * The only remaining REST endpoints are for the File Browser,
 * which is served by the gRPC server's EngineService (ListRepos, etc.)
 * and accessed via gRPC-Web, not REST.
 *
 * This file is kept for reference only and should be removed in a future cleanup.
 */

// ── File Browser endpoints (still using REST temporarily) ────────
// These will be migrated to gRPC-Web EngineService in a follow-up.

import type {
  ReposData,
  DirectoryListing,
  FileContent,
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
    const json = JSON.parse(text);
    detail = json.error ?? json.message ?? json.detail ?? text;
  } catch {
    detail = res.statusText || `HTTP ${res.status}`;
  }
  throw new Error(`API error ${res.status}: ${detail}`);
}

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) await throwApiError(res);
  return res.json() as Promise<T>;
}

// ── File Browser endpoints (still REST, served by gRPC server) ──

export function getRepos() {
  return fetchJson<ReposData>(`${BASE}/repos`);
}

export function getRepoTree(repoId: string, path = "") {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  return fetchJson<DirectoryListing>(`${BASE}/repos/${repoId}/tree?${params}`);
}

export function getRepoFile(repoId: string, path: string) {
  const params = new URLSearchParams();
  params.set("path", path);
  return fetchJson<FileContent>(`${BASE}/repos/${repoId}/file?${params}`);
}
