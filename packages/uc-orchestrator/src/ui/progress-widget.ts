/**
 * SubtaskProgressWidget — rich progress display above OMP editor.
 *
 * Replaces the plain-text updateWidget() strings with a Component
 * factory that renders styled subtask status, wave progress, and running
 * subtask names.
 *
 * Used via: ctx.ui.setWidget("uc-progress", widgetFactory)
 */

import type { TaskState } from "../orchestrator/orchestrator";
import type { Theme } from "@oh-my-pi/pi-coding-agent";
import type { Component } from "@oh-my-pi/pi-tui";
import { formatErrorForDisplay } from "./error-format";

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

// ── Progress Bar ─────────────────────────────────────────────────

function progressBar(completed: number, total: number, width: number, theme: Theme): string {
	if (total === 0) return "";
	const ratio = completed / total;
	const filled = Math.round(ratio * width);
	const empty = width - filled;
	return theme.fg("success", "█".repeat(filled)) + theme.fg("dim", "░".repeat(empty));
}

// ── Single-subtask percent bar ───────────────────────────────────

/** Compact inline progress bar for a single subtask's percent (0-100). */
function percentBar(percent: number, width: number, theme: Theme): string {
	const clamped = Math.max(0, Math.min(100, percent));
	const filled = Math.round((clamped / 100) * width);
	const empty = width - filled;
	return theme.fg("accent", "█".repeat(filled)) + theme.fg("dim", "░".repeat(empty));
}

// ── Widget Component ─────────────────────────────────────────────

export interface ProgressWidgetState {
	task: TaskState;
	waveIdx?: number;
	totalWaves?: number;
	/**
	 * Live subtask progress keyed by subtaskId (from subtask_progress events via
	 * WatchTask). Used by the widget to show phase/percent/agent for running
	 * subtasks. PR4 renders this; PR3 just stores it.
	 */
	progressBySubtask?: Map<string, SubtaskProgressInfo>;
}

/** Real-time progress for a single subtask (phase/percent/agent). */
export interface SubtaskProgressInfo {
	phase: string;
	percent: number;
	stepIndex?: number;
	stepTotal?: number;
	stepAgent?: string;
	stepStatus?: string;
	stepSummary?: string;
}

export function createProgressWidget(state: () => ProgressWidgetState | null) {
	// Return a Component factory compatible with ctx.ui.setWidget()
	return (tui: unknown, theme: Theme): Component & { dispose?(): void } => {
		return new ProgressWidgetComponent(state, theme);
	};
}

class ProgressWidgetComponent {
	private lastRender: string[] = [];

	constructor(
		private state: () => ProgressWidgetState | null,
		private theme: Theme,
	) {}

	render(width: number): string[] {
		const s = this.state();
		if (!s) {
			const result = [this.theme.fg("dim", "  UC: idle")];
			this.lastRender = result;
			return result;
		}

		const { task, waveIdx, totalWaves } = s;
		const lines: string[] = [];

		// Header: task ID + status
		const ctrl = task.controlState !== "running" ? ` [${task.controlState}]` : "";
		const statusColor = task.status === "completed" ? "success" : task.status === "failed" ? "error" : "accent";
		lines.push(
			`  ${this.theme.fg("accent", "UC")} ${this.theme.fg("dim", task.id.slice(0, 12))} ${this.theme.fg(statusColor, task.status)}${ctrl}`,
		);

		// Wave progress
		if (waveIdx !== undefined && totalWaves !== undefined && totalWaves > 0) {
			const completed = task.subtasks.filter((s) => s.status === "completed").length;
			const total = task.subtasks.length;
			const bar = progressBar(completed, total, Math.min(width - 20, 30), this.theme);
			lines.push(
				`  ${this.theme.fg("dim", `Wave ${waveIdx + 1}/${totalWaves}`)} ${bar} ${completed}/${total}`,
			);
		}

		// Running subtasks
		const running = task.subtasks.filter((s) => s.status === "running" || s.status === "reviewing");
		if (running.length > 0) {
			for (const st of running.slice(0, 3)) {
				const icon = statusIcon(st.status, this.theme);
				const desc = st.description.slice(0, width - 12);
				lines.push(`  ${icon} ${this.theme.fg("dim", st.id)}: ${desc}`);

				// Live progress (phase / percent / agent) from subtask_progress events
				const prog = s.progressBySubtask?.get(st.id);
				if (prog) {
					const barWidth = Math.min(16, Math.max(6, Math.floor((width - 30) / 2)));
					const bar = percentBar(prog.percent, barWidth, this.theme);
					const agentBadge = prog.stepAgent
						? ` ${this.theme.fg("accent", `[${prog.stepAgent}]`)}`
						: "";
					const phaseLabel = prog.phase ? this.theme.fg("dim", prog.phase) : "";
					const percentLabel = this.theme.fg("accent", `${prog.percent}%`);
					lines.push(`    ${agentBadge} ${phaseLabel} ${bar} ${percentLabel}`.replace(/\s+/g, " ").trimStart());
				}
			}
			if (running.length > 3) {
				lines.push(`  ${this.theme.fg("dim", `  ...+${running.length - 3} more`)}`);
			}
		}

		// Failed subtasks summary — show IDs plus first error for quick diagnosis
		const failed = task.subtasks.filter((s) => s.status === "failed");
		if (failed.length > 0) {
			lines.push(`  ${this.theme.fg("error", `⚠ ${failed.length} failed:`)} ${failed.map((s) => s.id).join(", ")}`);
			// Show first failed subtask's error (truncated root cause, friendly label)
			const firstErr = failed.find((s) => s.error);
			if (firstErr && firstErr.error) {
				lines.push(`  ${formatErrorForDisplay(firstErr.error, width - 6, (c, t) => this.theme.fg(c, t))}`);
			}
		}

		this.lastRender = lines;
		return lines;
	}

	invalidate(): void {
		this.lastRender = [];
	}

	dispose(): void {}
}
