/**
 * Runtime-surface plumbing for the product overview (`src/pages/HomePage.tsx`).
 *
 * The overview is also published as a public, static showcase: a plain nginx
 * host serves the SPA shell for any path, so `/dashboard/api/health` can answer
 * with HTML or a 502 instead of the Gateway's snapshot. The page used to mount
 * `useGrpcWeb({ enabled: true })` unconditionally, which opened a WatchTask
 * stream and retried forever against a backend that was never going to answer.
 *
 * Everything below is pure so `tests/homeRuntime.test.mjs` can pin the
 * behaviour without a browser: the probe classifier, the poll schedule, the
 * mode/badge mapping and the sync-age formatter.
 */

/** Endpoint probed before any gRPC-Web traffic is attempted. */
export const RUNTIME_PROBE_PATH = "/dashboard/api/health";
/** Abort the probe after this long — a hanging backend must not stall the page. */
export const RUNTIME_PROBE_TIMEOUT_MS = 2500;
/** Steady-state refresh cadence once the Gateway has answered. */
export const RUNTIME_POLL_INTERVAL_MS = 30_000;
/** First backoff step after a failed poll. */
export const RUNTIME_RETRY_BASE_MS = 15_000;
/** Backoff ceiling. */
export const RUNTIME_RETRY_MAX_MS = 120_000;
/** Consecutive poll failures after which polling stops instead of hammering. */
export const RUNTIME_MAX_FAILURES = 3;
/** Document title while the overview is mounted. */
export const HOME_DOCUMENT_TITLE = "UltimateCoders — distributed AI coding";

/** `probing` is the pre-probe state; the other two are the probe's verdicts. */
export type ProbeState = "probing" | "reachable" | "absent";
/** What the runtime card is allowed to claim. */
export type ShowcaseRuntimeMode = "checking" | "connecting" | "live" | "preview";
export type ShowcaseRuntimeBadge = "CHECKING" | "SYNCING" | "LIVE" | "PREVIEW";
/** The subset of `useGrpcWeb`'s connection state this page reacts to. */
export type TransportState = "connected" | "connecting" | "reconnecting" | "error" | "disconnected";

/**
 * A SPA fallback answers `/dashboard/api/health` with 200 `text/html`, and a
 * missing upstream answers 502 with an nginx error page. Only a 2xx response
 * that actually claims JSON may be treated as "the Gateway is here".
 */
export function classifyProbeResponse(status: number, contentType: string | null): "reachable" | "absent" {
  const isSuccess = status >= 200 && status < 300;
  const claimsJson = (contentType ?? "").toLowerCase().includes("application/json");
  return isSuccess && claimsJson ? "reachable" : "absent";
}

/** Probe the dashboard API. Never throws: a network error means "absent". */
export async function probeGateway(
  fetchImpl: typeof fetch = fetch,
  signal?: AbortSignal,
): Promise<"reachable" | "absent"> {
  try {
    const response = await fetchImpl(RUNTIME_PROBE_PATH, {
      signal,
      headers: { accept: "application/json" },
      credentials: "same-origin",
    });
    return classifyProbeResponse(response.status, response.headers.get("content-type"));
  } catch {
    // Abort or transport failure: the Gateway cannot serve this page.
    return "absent";
  }
}

/**
 * The card may only say LIVE when the transport is up *and* nothing is
 * currently failing; an unknown probe means CHECKING, an absent one PREVIEW.
 */
export function showcaseRuntimeMode(input: {
  probe: ProbeState;
  connected: boolean;
  consecutiveFailures: number;
}): ShowcaseRuntimeMode {
  if (input.probe === "probing") return "checking";
  if (input.probe === "absent") return "preview";
  if (input.connected && input.consecutiveFailures === 0) return "live";
  return "connecting";
}

export function runtimeBadge(mode: ShowcaseRuntimeMode): ShowcaseRuntimeBadge {
  switch (mode) {
    case "live":
      return "LIVE";
    case "connecting":
      return "SYNCING";
    case "preview":
      return "PREVIEW";
    default:
      return "CHECKING";
  }
}

export function runtimeStatusLabel(mode: ShowcaseRuntimeMode): string {
  switch (mode) {
    case "live":
      return "Gateway transport connected";
    case "connecting":
      return "Connecting to Gateway transport";
    case "preview":
      return "Static preview · no Gateway behind this host";
    default:
      return "Checking for a Gateway";
  }
}

export function transportLabel(state: TransportState): string {
  switch (state) {
    case "connected":
      return "Gateway transport connected";
    case "connecting":
      return "Connecting to Gateway transport";
    case "reconnecting":
      return "Reconnecting to Gateway transport";
    case "error":
      return "Gateway needs attention";
    default:
      return "Gateway transport disconnected";
  }
}

/** 0 (healthy) → the steady cadence; each failure doubles the wait, capped. */
export function nextPollDelay(consecutiveFailures: number): number {
  if (consecutiveFailures <= 0) return RUNTIME_POLL_INTERVAL_MS;
  const backoff = RUNTIME_RETRY_BASE_MS * 2 ** (consecutiveFailures - 1);
  return Math.min(RUNTIME_RETRY_MAX_MS, backoff);
}

/** Stop polling entirely rather than retry forever on a dead origin. */
export function shouldStopPolling(consecutiveFailures: number): boolean {
  return consecutiveFailures >= RUNTIME_MAX_FAILURES;
}

/** Human-readable freshness; a clock skew in the past reads as "just now". */
export function formatSyncAge(timestamp: number | null, now: number): string {
  if (timestamp === null) return "waiting for first sync";
  const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}
