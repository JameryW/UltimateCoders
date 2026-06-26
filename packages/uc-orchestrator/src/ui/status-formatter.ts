/**
 * StatusFormatter — structured /uc status output.
 *
 * Replaces plain-text notify() output with styled rendering
 * using Theme colors for status icons, progress, and DAG visualization.
 */

import type { Theme } from "@oh-my-pi/pi-coding-agent";
import type { TaskState, SubtaskResult } from "../orchestrator/orchestrator";

// ── Status Icons ─────────────────────────────────────────────────

const STATUS_ICON: Record<string, (theme: Theme) => string> = {
	completed: (t) => t.fg("success", "✓"),
	running: (t) => t.fg("warning", "●"),
	reviewing: (t) => t.fg("accent", "◉"),
	failed: (t) => t.fg("error", "✗"),
	cancelled: (t) => t.fg("dim", "⊘"),
	pending: (t) => t.fg("dim", "○"),
	planning: (t) => t.fg("dim", "◎"),
};

function statusIcon(status: string, theme: Theme): string {
	return (STATUS_ICON[status] ?? STATUS_ICON.pending)(theme);
}

// ── Task List (no task ID) ───────────────────────────────────────

export function formatTaskList(tasks: TaskState[], theme: Theme): string[] {
	if (tasks.length === 0) return [theme.fg("dim", "No tasks")];

	const lines: string[] = [];
	for (const task of tasks) {
		const icon = statusIcon(task.status, theme);
		const completed = task.subtasks.filter((s) => s.status === "completed").length;
		const total = task.subtasks.length;
		const ctrl = task.controlState !== "running" ? ` [${task.controlState}]` : "";
		lines.push(`${icon} ${task.id.slice(0, 14)} ${completed}/${total} ${task.status}${ctrl}`);
		lines.push(theme.fg("dim", `  ${task.description.slice(0, 60)}`));
	}
	return lines;
}

// ── Task Detail (with task ID) ───────────────────────────────────

export function formatTaskDetail(task: TaskState, theme: Theme): string[] {
	const lines: string[] = [];
	const icon = statusIcon(task.status, theme);

	lines.push(`${icon} ${theme.bold(task.id)} — ${task.status}`);
	lines.push(theme.fg("dim", `  Description: ${task.description}`));
	if (task.error) {
		lines.push(theme.fg("error", `  Error: ${task.error.slice(0, 100)}`));
	}

	lines.push("");
	lines.push(theme.fg("accent", "Subtasks:"));

	// Group by dependency depth for simple tree visualization
	const byDeps = new Map<number, SubtaskResult[]>();
	for (const st of task.subtasks) {
		const depth = st.dependsOn.length;
		if (!byDeps.has(depth)) byDeps.set(depth, []);
		byDeps.get(depth)!.push(st);
	}

	for (const [depth, subtasks] of [...byDeps.entries()].sort(([a], [b]) => a - b)) {
		for (const st of subtasks) {
			const stIcon = statusIcon(st.status, theme);
			const indent = "  ".repeat(depth + 1);
			const prefix = depth > 0 ? "↳ " : "";
			const deps = st.dependsOn.length > 0
				? theme.fg("dim", ` ←${st.dependsOn.join(",")}`)
				: "";
			lines.push(`${indent}${stIcon} ${prefix}${st.id}: ${st.description.slice(0, 50)}${deps}`);

			if (st.error) {
				lines.push(theme.fg("error", `${indent}  ⚠ ${st.error.slice(0, 60)}`));
			}
			if (st.retryCount && st.retryCount > 0) {
				lines.push(theme.fg("dim", `${indent}  Retries: ${st.retryCount}`));
			}
		}
	}

	return lines;
}
