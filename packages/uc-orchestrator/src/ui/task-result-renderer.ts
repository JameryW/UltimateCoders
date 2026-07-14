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

export function createTaskResultRenderer(): (message: any, options: { expanded: boolean }, theme: Theme) => Component | undefined {
	return (message, options, theme) => {
		const details: TaskResultDetails | undefined = message.details;
		if (!details) return undefined;

		const summaryLines: string[] = [];
		// Summary header
		const statusColor = details.status === "completed" ? "success" : details.status === "failed" ? "error" : "dim";
		summaryLines.push(
			theme.fg(statusColor, `■ Task ${details.taskId.slice(0, 12)}`) +
			theme.fg("dim", ` — ${details.status} — ${details.subtaskCount} subtask(s)`),
		);

		// ponytail: capture raw subtask data at factory time; slice to width inside
		// render(width) since the factory closure doesn't receive the terminal width.
		const expandedSubtasks = options.expanded && details.task
			? details.task.subtasks.map((st) => ({
				icon: statusIcon(st.status, theme),
				id: st.id,
				desc: st.description,
				error: st.error,
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
				}
				return lines;
			},
			invalidate: () => {},
		};
	};
}
