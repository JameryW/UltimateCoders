/**
 * TaskResultRenderer — custom message renderer for uc-task-result messages.
 *
 * Registers via: pi.registerMessageRenderer("uc-task-result", renderer)
 * Renders task completion messages as styled Components with summary,
 * subtask status icons, and expand/collapse.
 */

import type { Component } from "@oh-my-pi/pi-tui";
import type { Theme } from "@oh-my-pi/pi-coding-agent";
import type { TaskState } from "../orchestrator/orchestrator";
import { formatErrorForDisplay } from "./error-format";
import { statusIcon } from "./status-icons";

// ── Message Details Type ─────────────────────────────────────────

interface TaskResultDetails {
	taskId: string;
	status: string;
	subtaskCount: number;
	task?: TaskState;
}

// ── Renderer ─────────────────────────────────────────────────────

// ── Helpers ──────────────────────────────────────────────────────

// ponytail: tally subtask statuses into the 4 buckets a completion summary
// cares about. Everything not in the map (planning/reviewing/in_progress on a
// terminal task, unknown statuses from a newer server) falls into "other".
type StatusCounts = { completed: number; failed: number; cancelled: number; other: number };
function countByStatus(statuses: string[]): StatusCounts {
	const c: StatusCounts = { completed: 0, failed: 0, cancelled: 0, other: 0 };
	for (const s of statuses) {
		if (s === "completed") c.completed++;
		else if (s === "failed") c.failed++;
		else if (s === "cancelled") c.cancelled++;
		else c.other++;
	}
	return c;
}

// ponytail: build "N subtask(s) (X done, Y failed)"-style suffix. Always leads
// with the total (preserves the old headline when all-buckets-but-done are 0),
// then appends non-zero buckets so a failed task surfaces the actionable count.
// Order: failed first (most actionable), then cancelled, then other, then done.
function breakdownSuffix(c: StatusCounts, total: number): string {
	const parts: string[] = [];
	if (c.failed > 0) parts.push(`${c.failed} failed`);
	if (c.cancelled > 0) parts.push(`${c.cancelled} cancelled`);
	if (c.other > 0) parts.push(`${c.other} other`);
	// done only when it's not the whole set (all-done = the total already says it)
	if (c.completed > 0 && c.completed < total) parts.push(`${c.completed} done`);
	return parts.length > 0 ? `${total} subtask(s) (${parts.join(", ")})` : `${total} subtask(s)`;
}

export function createTaskResultRenderer(getTask?: (taskId: string) => TaskState | undefined): (message: any, options: { expanded: boolean }, theme: Theme) => Component | undefined {
	return (message, options, theme) => {
		const details: TaskResultDetails | undefined = message.details;
		if (!details) return undefined;

		const summaryLines: string[] = [];
		// ponytail: capture raw subtask data at factory time; slice to width inside
		// render(width) since the factory closure doesn't receive the terminal width.
		// F15: the emitter only sends {taskId, status, subtaskCount} — details.task
		// is never populated, so resolve live via the getter (extension passes
		// orchestrator.getTaskState). Evicted tasks degrade to the header only.
		const task = details.task ?? getTask?.(details.taskId);
		// ponytail: breakdown of how the N subtasks landed — for a failed task,
		// "failed — 5 subtask(s)" hides that 3 succeeded and 2 failed (the 2 are the
		// actionable ones). task already resolved above; evicted → bare suffix.
		const counts = task ? countByStatus(task.subtasks.map((s) => s.status)) : null;
		const suffix = counts
			? breakdownSuffix(counts, details.subtaskCount)
			: `${details.subtaskCount} subtask(s)`;
		// Summary header
		const statusColor = details.status === "completed" ? "success" : details.status === "failed" ? "error" : "dim";
		summaryLines.push(
			theme.fg(statusColor, `■ Task ${details.taskId.slice(0, 12)}`) +
			theme.fg("dim", ` — ${details.status} — ${suffix}`),
		);

		const expandedSubtasks = options.expanded && task
			? task.subtasks.map((st) => ({
				icon: statusIcon(st.status, theme),
				id: st.id,
				desc: st.description,
				error: st.error,
				result: st.result,
				review: st.review,
			}))
			: [];

		return {
			render: (width: number): string[] => {
				const lines = [...summaryLines];
				for (const st of expandedSubtasks) {
					const desc = st.desc.slice(0, Math.max(0, width - st.id.length - 6));
					lines.push(`  ${st.icon} ${st.id}: ${desc}`);
					if (st.error) {
						lines.push(`    ${formatErrorForDisplay(st.error, Math.max(0, width - 4), (c, t) => theme.fg(c, t))}`);
					}
					// ponytail: success output — the expanded message showed a subtask's
					// error + review but NOT its result (what it actually produced). The
					// subtask-tree overlay shows the result; mirror it here: first line +
					// `…` when there's more (multi-line or width-clamped). Error first
					// (diagnostic priority); a subtask has one or the other, not both.
					if (!st.error && st.result) {
						const budget = Math.max(0, width - 4);
						const firstLine = st.result.split("\n")[0];
						const moreLines = st.result.includes("\n");
						const truncated = firstLine.length > budget - 1 || moreLines;
						const shown = truncated
							? firstLine.slice(0, Math.max(0, budget - 1)) + "…"
							: firstLine.slice(0, budget);
						lines.push(theme.fg("dim", `    ${shown}`));
					}
					// ponytail: review verdict — the subtask-tree overlay + /uc status
					// detail show ✓/✗ approved/rejected; the expanded completion message
					// omitted it. A rejected subtask is why a "completed" task may still
					// need attention. Mirror the label; no review → no line.
					if (st.review) {
						const label = st.review.approved ? "✓ approved" : "✗ rejected";
						lines.push(theme.fg(st.review.approved ? "success" : "error", `    ${label}`));
					}
				}
				return lines;
			},
			invalidate: () => {},
		};
	};
}
