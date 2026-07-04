/**
 * Exponential backoff helper for reconnect/retry loops.
 *
 * Shared by GrpcBridge (RPC reconnect) and UCOrchestrator (WatchTask stream
 * reconnect) so both follow the same backoff curve. Backoff is connection-
 * error only — business errors fail fast and never reach this helper.
 *
 * Curve: 500ms → 1s → 2s → 4s → 8s → 16s → 30s (cap), with optional jitter.
 * After `maxAttempts` steps the helper returns null (caller stops retrying).
 */

export interface BackoffOptions {
	/** Initial delay in ms. Default 500. */
	initialMs?: number;
	/** Maximum delay cap in ms. Default 30_000. */
	maxMs?: number;
	/** Maximum number of attempts (delays) before giving up. Default 5. */
	maxAttempts?: number;
	/** Jitter factor 0..1. Default 0 (no jitter). Each delay is multiplied by (1 ± factor). */
	jitter?: number;
}

/**
 * Compute the delay for attempt `attempt` (0-indexed).
 * Returns null when `attempt >= maxAttempts`.
 */
export function backoffDelay(attempt: number, opts: BackoffOptions = {}): number | null {
	const initialMs = opts.initialMs ?? 500;
	const maxMs = opts.maxMs ?? 30_000;
	const maxAttempts = opts.maxAttempts ?? 5;
	const jitter = opts.jitter ?? 0;

	if (attempt >= maxAttempts) return null;
	// exponential: initial * 2^attempt, capped
	const raw = Math.min(initialMs * 2 ** attempt, maxMs);
	if (jitter <= 0) return raw;
	const factor = 1 + (Math.random() * 2 - 1) * jitter;
	return Math.max(0, Math.round(raw * factor));
}

/**
 * Sleep for the backoff delay of `attempt`. Returns true if it slept, false
 * if `attempt >= maxAttempts` (caller should give up).
 */
export async function sleepBackoff(attempt: number, opts: BackoffOptions = {}): Promise<boolean> {
	const delay = backoffDelay(attempt, opts);
	if (delay === null) return false;
	await new Promise<void>((resolve) => {
		const t = setTimeout(resolve, delay);
		// Allow the Node/Bun event loop to exit promptly during teardown.
		if (typeof t === "object" && t && "unref" in t && typeof (t as { unref: () => void }).unref === "function") {
			(t as { unref: () => void }).unref();
		}
	});
	return true;
}
