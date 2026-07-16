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
import { statusIcon } from "./status-icons";

// ── Progress Bar ─────────────────────────────────────────────────

function progressBar(completed: number, total: number, width: number, theme: Theme): string {
	if (total === 0 || width <= 0) return "";
	const ratio = completed / total;
	const filled = Math.round(ratio * width);
	const empty = width - filled;
	return theme.fg("success", "█".repeat(filled)) + theme.fg("dim", "░".repeat(empty));
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
	parallelGroup?: string;
	parallelStepCount?: number;
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

		// Header: task ID + status + description (what the task IS, not just a UUID)
		const ctrl = task.controlState !== "running" ? ` [${task.controlState}]` : "";
		const statusColor = task.status === "completed" ? "success" : task.status === "failed" ? "error" : "accent";
		const idStr = task.id.slice(0, 12);
		// ponytail: budget the description by the PLAIN-text prefix length (ANSI is
		// applied after slicing, mirroring the failed-IDs line) - no pi-tui value
		// imports are available for ANSI-aware truncation. Without the description,
		// the always-visible widget showed only a truncated UUID, not what the task
		// was about; /uc status showed it but the glanceable view did not.
		const prefixPlain = `  UC ${idStr} ${task.status}${ctrl}`;
		const descBudget = Math.max(0, width - prefixPlain.length - 3); // 3 for " - "
		const desc = task.description.slice(0, descBudget);
		lines.push(
			`  ${this.theme.fg("accent", "UC")} ${this.theme.fg("dim", idStr)} ${this.theme.fg(statusColor, task.status)}${ctrl}${desc ? this.theme.fg("dim", ` - ${desc}`) : ""}`,
		);

		// Wave progress
		if (waveIdx !== undefined && totalWaves !== undefined && totalWaves > 0) {
			const completed = task.subtasks.filter((s) => s.status === "completed").length;
			const total = task.subtasks.length;
			const bar = progressBar(completed, total, Math.max(0, Math.min(width - 20, 30)), this.theme);
			lines.push(
				`  ${this.theme.fg("dim", `Wave ${waveIdx + 1}/${totalWaves}`)} ${bar} ${completed}/${total}`,
			);
		}

		// Running subtasks
		const running = task.subtasks.filter((s) => s.status === "running" || s.status === "reviewing");
		if (running.length > 0) {
			for (const st of running.slice(0, 3)) {
				const icon = statusIcon(st.status, this.theme);
				const desc = st.description.slice(0, Math.max(0, width - 12));
				lines.push(`  ${icon} ${this.theme.fg("dim", st.id)}: ${desc}`);
				// Render live step progress (agent + phase + percent + status tag) when available
				const prog = s.progressBySubtask?.get(st.id);
				if (prog) {
					const agentTag = prog.stepAgent ? this.theme.fg("accent", prog.stepAgent) : "";
					const phaseText = prog.phase ? this.theme.fg("dim", prog.phase.slice(0, Math.max(0, width - 16))) : "";
					// ponytail: percent + stepIndex/stepTotal were populated by the
					// subtask_progress event but never rendered — dead data. Show them.
					const pctTag = prog.percent >= 0 ? this.theme.fg("warning", `${prog.percent}%`) : "";
					const stepTag =
						prog.stepIndex !== undefined && prog.stepTotal !== undefined && prog.stepTotal > 0
							? this.theme.fg("dim", `[${prog.stepIndex}/${prog.stepTotal}]`)
							: "";
					const statusTag = prog.stepStatus ? this._stepStatusTag(prog.stepStatus) : "";
					const parallelTag =
						prog.parallelGroup && prog.parallelStepCount && prog.parallelStepCount > 1
							? this.theme.fg("warning", `↻${prog.parallelStepCount} parallel`)
							: "";
					const parts = ["    ", agentTag, pctTag, stepTag, phaseText, statusTag, parallelTag].filter(Boolean);
					if (parts.length > 1) lines.push(parts.join(" "));
					// ponytail: stepSummary is the human-readable current-step text
					// (populated by subtask_progress, was dead data). Show on its own
					// dim line, truncated to width, so the tag line stays scannable.
					if (prog.stepSummary) {
						lines.push(this.theme.fg("dim", `      ${prog.stepSummary.slice(0, Math.max(0, width - 6))}`));
					}
				}
			}
			if (running.length > 3) {
				lines.push(`  ${this.theme.fg("dim", `  ...+${running.length - 3} more`)}`);
			}
		}

		// Failed subtasks summary — show IDs plus first error for quick diagnosis
		const failed = task.subtasks.filter((s) => s.status === "failed");
		if (failed.length > 0) {
			// ponytail: prefix eats ~12 cols (`  ⚠ N failed: `); truncate ID list to
			// the remaining width so a long failure set doesn't wrap the line.
			const prefix = `  ⚠ ${failed.length} failed: `;
			const idBudget = Math.max(0, width - prefix.length - 2);
			let idList = failed.map((s) => s.id).join(", ");
			if (idList.length > idBudget) {
				idList = idBudget > 0 ? idList.slice(0, idBudget - 1) + "…" : "";
			}
			lines.push(`${this.theme.fg("error", prefix)}${idList}`);
			// Show first failed subtask's error (truncated root cause, friendly label)
			const firstErr = failed.find((s) => s.error);
			if (firstErr && firstErr.error) {
				lines.push(`  ${formatErrorForDisplay(firstErr.error, width - 6, (c, t) => this.theme.fg(c, t))}`);
				// ponytail: S8 — show retry count on a separate dim line when the
				// first failed subtask was retried. formatErrorForDisplay reads
				// st.error (pure root cause, no retry prefix for remote subtasks),
				// so the retry count wouldn't be visible otherwise. Only shown when
				// retryCount > 0 to avoid noise on first-attempt failures.
				if (firstErr.retryCount && firstErr.retryCount > 0) {
					lines.push(this.theme.fg("dim", `  retried ${firstErr.retryCount}×`));
				}
			}
		}

		this.lastRender = lines;
		return lines;
	}

	invalidate(): void {
		this.lastRender = [];
	}

	/** Map a workflow step_status to a colored tag for the progress widget. */
	private _stepStatusTag(status: string): string {
		switch (status) {
			case "retrying":
				return this.theme.fg("warning", "[retry]");
			case "skipped":
				return this.theme.fg("dim", "[skip]");
			case "failed":
				return this.theme.fg("error", "[fail]");
			default:
				// "started"/"completed" are implied by the phase — no tag
				return "";
		}
	}

	dispose(): void {}
}
