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
import { formatElapsed } from "./elapsed";

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
	/** -1 = worker reported no data (widget skips negatives; never shows a bogus 0%). */
	percent: number;
	stepIndex?: number;
	stepTotal?: number;
	stepAgent?: string;
	stepStatus?: string;
	stepSummary?: string;
	parallelGroup?: string;
	parallelStepCount?: number;
	/**
	 * ponytail: F19 — when this subtask was first seen running (seeded at
	 * subtask_start, carried across subtask_progress updates). Drives the
	 * elapsed tag so a hung subtask (frozen %/phase) is distinguishable from
	 * an active one. Client-side timestamp — zero protocol change.
	 */
	firstSeen?: number;
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
					// ponytail: S9 — the tag line joins [agent, pct, step, status,
					// parallel, phase] with spaces. Previously each tag was built
					// independently (phase got `width-16` budget as if standalone)
					// then joined — the joined line could exceed `width`, and on
					// narrow terminals the compositor ANSI-truncated the RIGHT
					// side, cutting parallelTag/statusTag (the most action-relevant
					// tags). Now we track each tag's PLAIN-text length alongside its
					// rendered (ANSI-themed) string, then greedily fit tags into the
					// line budget, dropping/truncating LOW-priority tags first.
					// Priority (HIGH→LOW, drop low first): agent > pct > step >
					// status > parallel > phase. phase is last so it's trimmed first.
					// We track plain widths manually because pi-tui value imports
					// (ANSI-aware width utils) crash at runtime per project memory.
					// NEVER raw-slice a rendered (ANSI) string — trimming happens on
					// plain content BEFORE applying theme.fg.
					const tags: { plain: string; rendered: string }[] = [];
					if (prog.stepAgent) tags.push({ plain: prog.stepAgent, rendered: this.theme.fg("accent", prog.stepAgent) });
					if (prog.percent >= 0) tags.push({ plain: `${prog.percent}%`, rendered: this.theme.fg("warning", `${prog.percent}%`) });
					if (prog.stepIndex !== undefined && prog.stepTotal !== undefined && prog.stepTotal > 0) {
						const plain = `[${prog.stepIndex}/${prog.stepTotal}]`;
						tags.push({ plain, rendered: this.theme.fg("dim", plain) });
					}
					if (prog.stepStatus) {
						const tag = this._stepStatusTag(prog.stepStatus);
						if (tag) {
							// _stepStatusTag returns themed string; plain is the tag text
							const plainMap: Record<string, string> = { retrying: "[retry]", skipped: "[skip]", failed: "[fail]" };
							tags.push({ plain: plainMap[prog.stepStatus] ?? prog.stepStatus, rendered: tag });
						}
					}
					if (prog.parallelGroup && prog.parallelStepCount && prog.parallelStepCount > 1) {
						const plain = `↻${prog.parallelStepCount} parallel`;
						tags.push({ plain, rendered: this.theme.fg("warning", plain) });
					}
					// ponytail: F19 — elapsed since firstSeen (subtask_start seed,
					// carried across progress updates). A hung subtask's %/phase
					// freeze; the ticking elapsed (refreshed on each event) separates
					// "running 9m, stuck" from "running 9m, fine". Second-lowest
					// priority: under tight budgets phase trims first, then this.
					if (prog.firstSeen !== undefined) {
						const plain = `(${formatElapsed(Date.now() - prog.firstSeen)})`;
						tags.push({ plain, rendered: this.theme.fg("dim", plain) });
					}
					// phase LAST — lowest priority, trimmed/dropped first
					if (prog.phase) tags.push({ plain: prog.phase, rendered: this.theme.fg("dim", prog.phase) });

					const prefix = "    ";
					const sep = " ";
					const budget = Math.max(0, width - prefix.length);
					const kept: string[] = [];
					let used = 0;
					for (let i = 0; i < tags.length; i++) {
						const t = tags[i];
						const isLast = i === tags.length - 1;
						const add = (kept.length > 0 ? sep.length : 0) + t.plain.length;
						if (used + add <= budget) {
							kept.push(t.rendered);
							used += add;
						} else if (isLast && prog.phase) {
							// Trim phase (the last tag) to fit remaining budget with ellipsis
							const remain = budget - used - (kept.length > 0 ? sep.length : 0);
							if (remain > 1) {
								kept.push(this.theme.fg("dim", prog.phase.slice(0, remain - 1) + "…"));
							}
							break;
						} else {
							// Skip this tag; if it's the last one, stop
							if (isLast) break;
						}
					}
					if (kept.length > 0) lines.push(prefix + kept.join(sep));
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
