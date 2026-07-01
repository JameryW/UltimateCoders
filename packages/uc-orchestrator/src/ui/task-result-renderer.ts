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

// ── Status Icons ─────────────────────────────────────────────────

const STATUS_ICON: Record<string, (theme: Theme) => string> = {
	completed: (t) => t.fg("success", "✓"),
	running: (t) => t.fg("warning", "●"),
	reviewing: (t) => t.fg("accent", "◉"),
	failed: (t) => t.fg("error", "✗"),
	cancelled: (t) => t.fg("dim", "⊘"),
	pending: (t) => t.fg("dim", "○"),
};

function statusIcon(status: string, theme: Theme): string {
	return (STATUS_ICON[status] ?? STATUS_ICON.pending)(theme);
}

// ── Message Details Type ─────────────────────────────────────────

interface TaskResultDetails {
	taskId: string;
	status: string;
	subtaskCount: number;
	task?: TaskState;
}

// ── Renderer ─────────────────────────────────────────────────────

export function createTaskResultRenderer(): (message: any, options: { expanded: boolean }, theme: Theme) => Component | undefined {
	return (message, options, theme) => {
		const details: TaskResultDetails | undefined = message.details;
		if (!details) return undefined;

		const lines: string[] = [];

		// Summary header
		const statusColor = details.status === "completed" ? "success" : details.status === "failed" ? "error" : "dim";
		lines.push(
			theme.fg(statusColor, `■ Task ${details.taskId.slice(0, 12)}`) +
			theme.fg("dim", ` — ${details.status} — ${details.subtaskCount} subtask(s)`),
		);

		// When expanded, show subtask details
		if (options.expanded && details.task) {
			for (const st of details.task.subtasks) {
				const icon = statusIcon(st.status, theme);
				const desc = st.description.slice(0, 80);
				lines.push(`  ${icon} ${st.id}: ${desc}`);
				if (st.error) {
					lines.push(`    ${formatErrorForDisplay(st.error, 70, (c, t) => theme.fg(c, t))}`);
				}
			}
		}

		// Return a Component that renders our lines
		return {
			render: () => lines,
			invalidate: () => {},
		};
	};
}
