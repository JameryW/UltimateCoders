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

// ponytail: 3-segment bar — completed (success/green), failed (error/red),
// pending (dim/empty). Previously failed subtasks rendered as empty ░ like
// pending, so a task stuck at 3/5 with 2 failed looked identical to one still
// running with 5 pending — the bar hid the failure at a glance. failed fills
// between completed and pending so it's visually distinct without a legend.
function progressBar(completed: number, failed: number, total: number, width: number, theme: Theme): string {
	if (total === 0 || width <= 0) return "";
	const done = Math.round((completed / total) * width);
	const fail = Math.round((failed / total) * width);
	// ponytail: cap done+fail at width — rounding on edge ratios (e.g. 2/3 each
	// rounds to 1+1=2 on a 3-wide bar) can overrun by one; the pending slot
	// absorbs the shortfall so the bar never exceeds `width` cols.
	const failClamped = Math.max(0, Math.min(fail, width - done));
	const empty = Math.max(0, width - done - failClamped);
	return theme.fg("success", "█".repeat(done))
		+ theme.fg("error", "█".repeat(failClamped))
		+ theme.fg("dim", "░".repeat(empty));
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
	// ponytail: F24 — removed the lastRender cache field (write-only, never
	// read; invalidate() cleared it but nothing consumed it).

	constructor(
		private state: () => ProgressWidgetState | null,
		private theme: Theme,
	) {}

	render(width: number): string[] {
		const s = this.state();
		if (!s) {
			// ponytail: the always-visible widget is a first-run user's first UC surface.
			// "UC: idle" named no submit path — mirror the overlay empty-state (#422).
			// Gated on width ≥ 50 (the hint is ~38 plain chars); under it, stay bare so
			// the compositor doesn't ANSI-truncate a half-hint off the left side.
			const hint = width >= 50 ? this.theme.fg("dim", " · /uc submit <desc>") : "";
			return [this.theme.fg("dim", "  UC: idle") + hint];
		}

		const { task, waveIdx, totalWaves } = s;
		const lines: string[] = [];

		// Header: task ID + status + description (what the task IS, not just a UUID)
		const ctrl = task.controlState !== "running" ? ` [${task.controlState}]` : "";
		// ponytail: paused tasks show a one-key affordance so the user doesn't have
		// to open an overlay to discover how to resume. `r` mirrors the task-list
		// overlay's resume quick-action. Only when controlState is paused — cancelled
		// is terminal (resume would error), running has no such action. Gated on
		// width ≥ 60: the hint is ~38 plain chars and would overflow a narrow header
		// (the compositor would ANSI-truncate the description off the left side);
		// under 60 the user still finds /uc resume via /uc help.
		// ponytail: fill the ACTUAL task id (prefix-resolvable) so the affordance is
		// copy-paste runnable, not a `<id>` placeholder the user still has to look up.
		// `r in overlay` is the one-key path; the /uc command covers users who prefer it.
		// ponytail: surface resume for FAILED tasks too — a failed task is resumable
		// (the orchestrator's resumeTask re-runs the failed wave / subtask), so the user
		// watching the widget sees the same one-key hint a paused task gets. Without it,
		// the only signal was the red status + failed-summary; the recovery path (which
		// IS available) stayed undiscovered until the user opened an overlay. Mirrors
		// the paused affordance exactly. Cancelled is terminal (resume errors) → skip.
		const resumeId = task.id.slice(0, 8);
		const showResumeAffordance = (task.controlState === "paused" || task.status === "failed") && width >= 60;
		// ponytail: name the RIGHT overlay + how to open it — `r` (resume) lives in the
		// task-list overlay (Ctrl+Shift+T), NOT the subtask-tree (Ctrl+T, whose `r`/`R`
		// RETRIES a subtask). The bare "r in overlay" was ambiguous; "r in task list"
		// (#506) named the surface but a new user still had to find Ctrl+Shift+T. Add
		// the shortcut so the overlay path is reachable without /uc help.
		const affordance = showResumeAffordance
			? this.theme.fg("dim", ` · /uc resume ${resumeId} · r in task list (Ctrl+Shift+T)`)
			: "";
		const statusColor = task.status === "completed" ? "success" : task.status === "failed" ? "error" : "accent";
		const idStr = task.id.slice(0, 12);
		// ponytail: budget the description by the PLAIN-text prefix length (ANSI is
		// applied after slicing, mirroring the failed-IDs line) - no pi-tui value
		// imports are available for ANSI-aware truncation. Without the description,
		// the always-visible widget showed only a truncated UUID, not what the task
		// was about; /uc status showed it but the glanceable view did not.
		const prefixPlain = `  UC ${idStr} ${task.status}${ctrl}`;
		const descBudget = Math.max(0, width - prefixPlain.length - 3); // 3 for " - "
		// ponytail: ellipsis on a truncated desc — the slice was bare, so a long
		// description cut silently with no signal there was more. Mirrors the overlay
		// rows (#449/#450). Reserve 1 col for "…" and append only when the desc
		// overflows the budget; a desc that fits stays verbatim.
		const fullDesc = task.description;
		const truncated = fullDesc.length > descBudget;
		const desc = truncated
			? fullDesc.slice(0, Math.max(0, descBudget - 1)) + (descBudget > 0 ? "…" : "")
			: fullDesc.slice(0, descBudget);
		// ponytail: total run duration on a terminal task — the time-signal sweep
		// (#421/#430/#433/#443) covered when it started/ended, but not how long it
		// ran. "⏱ 5m" answers "did this take unusually long" at a glance, computed
		// from createdAt→completedAt. Only terminal tasks w/ completedAt; in-flight
		// shows the live age already. Dim so it reads as metadata, not a field.
		const durTag = task.completedAt && ["completed", "failed", "cancelled"].includes(task.status)
			? this.theme.fg("dim", ` · ⏱${formatElapsed(task.completedAt - task.createdAt)}`) : "";
		lines.push(
			`  ${this.theme.fg("accent", "UC")} ${this.theme.fg("dim", idStr)} ${this.theme.fg(statusColor, task.status)}${ctrl}${desc ? this.theme.fg("dim", ` - ${desc}`) : ""}${durTag}${affordance}`,
		);

		// ponytail: progress bar — show whenever there are subtasks, not only when
		// wave info is present. Restored/resumed tasks (or single-wave tasks with
		// missing wave data) had NO bar despite completed/total being computable,
		// so the glanceable widget lost its most useful element. Wave X/Y is now
		// optional trailing context (appended only when waveIdx/totalWaves exist).
		const total = task.subtasks.length;
		// ponytail: blocked-subtask detection shared by the bar tag (·N⏳, #488)
		// and the blocked summary line below. Deps checked against the task's own
		// subtask statuses (pending w/ unmet deps = blocked).
		const subById = new Map(task.subtasks.map((s) => [s.id, s]));
		const blockedSubs = task.subtasks.filter((s) =>
			s.status === "pending" && s.dependsOn.length > 0 &&
			s.dependsOn.some((depId) => { const dep = subById.get(depId); return !dep || dep.status !== "completed"; })
		);
		if (total > 0) {
			const completed = task.subtasks.filter((s) => s.status === "completed").length;
			const failed = task.subtasks.filter((s) => s.status === "failed").length;
			const bar = progressBar(completed, failed, total, Math.max(0, Math.min(width - 20, 30)), this.theme);
			// ponytail: F24 — the bar counts TASK-wide completion; leading with
			// "Wave X/Y" read as if the bar measured the current wave. Progress
			// first, wave identity as trailing context.
			const waveTag = (waveIdx !== undefined && totalWaves !== undefined && totalWaves > 0)
				? this.theme.fg("dim", ` · wave ${waveIdx + 1}/${totalWaves}`)
				: "";
			// ponytail: failed-count marker next to the count text — the bar shows the
			// failed segment in red (#427), but the count text "3/5" didn't surface the
			// number. Append " ·N✗" only when failed > 0 (no noise on healthy tasks).
			const failTag = failed > 0 ? this.theme.fg("error", ` ·${failed}✗`) : "";
			// ponytail: running-count marker — mirrors the subtask-tree/task-list headers
			// (#458/#459). The bar row showed completed/failed but not how many subtasks
			// are actively running, so "is this making progress" needed scanning the
			// running-subtask rows below. accent-colored "·N▶" only when running>0.
			const running = task.subtasks.filter((s) => s.status === "running" || s.status === "reviewing").length;
			const runTag = running > 0 ? this.theme.fg("accent", ` ·${running}▶`) : "";
			// ponytail: blocked-count marker — mirrors the task-list/subtask-tree headers
			// (#487/#486) + detail countTag (#485). The bar row showed completed/failed/
			// running but not how many subtasks are stalled on unmet deps. dim "·N⏳" only
			// when blocked>0.
			const blockTag = blockedSubs.length > 0 ? this.theme.fg("dim", ` ·${blockedSubs.length}⏳`) : "";
			lines.push(
				`  ${bar} ${completed}/${total}${failTag}${runTag}${blockTag}${waveTag}`,
			);
		}

		// Running subtasks
		const running = task.subtasks.filter((s) => s.status === "running" || s.status === "reviewing");
		if (running.length > 0) {
			for (const st of running.slice(0, 3)) {
				const icon = statusIcon(st.status, this.theme);
				// ponytail: F24 — budget by the actual prefix ("  " + icon visible 1
				// + " " + id + ": "). The old width-12 assumed short st-N ids, but
				// the planner accepts LLM-chosen ids, which overflowed the line.
				// ponytail: ellipsis on a truncated desc — the slice was bare, so a long
				// description cut silently. Mirrors the header + overlay rows (#449/#450/#451).
				const budget = Math.max(0, width - 6 - st.id.length);
				const fullDesc = st.description;
				const truncated = fullDesc.length > budget;
				const desc = truncated
					? fullDesc.slice(0, Math.max(0, budget - 1)) + (budget > 0 ? "…" : "")
					: fullDesc.slice(0, budget);
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
					// ponytail: multi-line or width-clamped stepSummary shows `…` — was a
					// bare slice with no indicator, so a multi-line summary looked complete
					// at line 1 (mirrors the subtask-tree result-ellipsis fix). Reserve 1
					// col for the ellipsis when truncating.
					if (prog.stepSummary) {
						const budget = Math.max(0, width - 6);
						const firstLine = prog.stepSummary.split("\n")[0];
						const moreLines = prog.stepSummary.includes("\n");
						const truncated = firstLine.length > budget - 1 || moreLines;
						const shown = truncated
							? firstLine.slice(0, Math.max(0, budget - 1)) + "…"
							: firstLine.slice(0, budget);
						lines.push(this.theme.fg("dim", `      ${shown}`));
					}
				}
			}
			if (running.length > 3) {
				lines.push(`  ${this.theme.fg("dim", `  ...+${running.length - 3} more`)}`);
			}
		}

		// Blocked subtasks summary — the bar shows "·N⏳" (#488) but not WHICH
		// subtasks are stalled; a task with 0 running + N blocked shows nothing
		// below the bar. Mirror the failed summary: one dim line with the IDs,
		// capped to width. Only when blocked > 0 (no noise on healthy tasks).
		// ponytail: prefix eats ~17 cols (`  ⏳ N blocked: `); truncate the ID list
		// to the remaining width so a long stalled set doesn't wrap the line.
		if (blockedSubs.length > 0) {
			const prefix = `  ⏳ ${blockedSubs.length} blocked: `;
			const idBudget = Math.max(0, width - prefix.length - 2);
			let idList = blockedSubs.map((s) => s.id).join(", ");
			if (idList.length > idBudget) {
				idList = idBudget > 0 ? idList.slice(0, idBudget - 1) + "…" : "";
			}
			lines.push(`${this.theme.fg("dim", prefix)}${idList}`);
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

		return lines;
	}

	invalidate(): void {}

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
