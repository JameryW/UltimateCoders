/**
 * Shared compact elapsed-time formatter for UC UI components.
 *
 * Single source of truth for "how long has this been running" tags — used by
 * the subtask-tree overlay (running rows) and the progress widget (tag line).
 * Extracted to prevent the two call sites drifting apart (the status-icons
 * extraction was motivated by exactly that).
 */

/** Compact elapsed: "42s" / "3m" / "1h 05m". Negative input clamps to 0. */
export function formatElapsed(ms: number): string {
	const s = Math.max(0, Math.floor(ms / 1000));
	if (s < 60) return `${s}s`;
	const m = Math.floor(s / 60);
	if (m < 60) return `${m}m`;
	return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}
