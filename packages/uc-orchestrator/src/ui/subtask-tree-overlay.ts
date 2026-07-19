/**
 * SubtaskTreeOverlay — interactive subtask tree overlay for OMP TUI.
 *
 * Opens via Ctrl+T shortcut. Shows subtask DAG with status icons,
 * dependency edges, and keyboard navigation.
 *
 * Uses: ui.custom(treeFactory, { overlay: true })
 */

import type { Component, TUI } from "@oh-my-pi/pi-tui";
import type { Theme } from "@oh-my-pi/pi-coding-agent";
import type { TaskState, SubtaskResult } from "../orchestrator/orchestrator";
import { formatErrorForDisplay } from "./error-format";
import { statusIcon } from "./status-icons";
import { overlayPageSize } from "./overlay-pagination";
// ponytail: F8/F19 — shared with progress-widget's elapsed tag (same format).
import { formatElapsed } from "./elapsed";

// ponytail: raw xterm key sequences — pi-tui value imports crash at runtime
// (vendor utils setNativeKillTree mismatch), so match bytes directly like the
// rest of the UC overlays. Coverage: arrows, page, home/end, enter, esc.
const KEY = {
	up: "\x1b[A",
	down: "\x1b[B",
	pageUp: "\x1b[5~",
	pageDown: "\x1b[6~",
	home: "\x1b[H",
	end: "\x1b[F",
	enter: "\r",
	esc: "\x1b",
};

// ── SubtaskTree Component ────────────────────────────────────────

export interface SubtaskTreeOptions {
	tasks: () => TaskState[];
	onRetry?: (taskId: string, subtaskId: string) => void;
	/** `d` jumps to the subtask's parent task detail (opens task-list overlay). */
	onJumpToTask?: (taskId: string) => void;
	/** Open with cursor on the first failed subtask (Ctrl+Shift+F jump-to-failed). */
	cursorOnFailed?: boolean;
	onClose: () => void;
}

export function createSubtaskTreeOverlay(opts: SubtaskTreeOptions) {
	return (tui: any, theme: Theme, _keybindings: unknown, done: (result: void) => void): Component & { dispose?(): void } => {
		return new SubtaskTreeComponent(opts, tui, theme, done);
	};
}

class SubtaskTreeComponent {
	private cursorIdx = 0;
	private expanded = new Set<string>();
	private flatItems: { taskId: string; subtask: SubtaskResult; depth: number }[] = [];
	private scrollOffset = 0;
	/**
	 * ponytail: height-adaptive page size (see overlayPageSize). Read live so a
	 * terminal resize takes effect on the next render/input without restart.
	 * Was a hardcoded 20 that overflowed short terminals, letting the
	 * maxHeight:"100%" clamp cut the footer + bottom cursor rows.
	 */
	private get maxVisible(): number {
		return overlayPageSize(this.tui);
	}
	// ponytail: stamp flatItems only when task count changes, not every render
	private lastSubtaskCount = -1;
	// ponytail: transient in-overlay hint for dead keys (e.g. r on non-failed).
	// Cleared on the next non-r/R keypress so any nav/enter/esc dismisses it.
	private flashMsg: string | null = null;
	// ponytail: search/filter mode — `/` enters editing, typing narrows the list,
	// Enter/nav exits editing but keeps the filter active, Esc clears everything.
	private searchMode = false;
	private query = "";
	// ponytail: F7 — 1s tick so time-based fields (running elapsed) re-render
	// while the overlay sits open. Cleared in dispose(); modal overlays are
	// short-lived, so 1fps redraw cost is negligible.
	private refreshTimer: ReturnType<typeof setInterval> | null = null;

	constructor(
		private opts: SubtaskTreeOptions,
		private tui: TUI,
		private theme: Theme,
		private done: (result: void) => void,
	) {
		this.refreshTimer = setInterval(() => (this.tui as any)?.requestRender?.(), 1000);
		this.rebuildItems();
		// ponytail: Ctrl+Shift-F jump-to-failed — pre-set cursor to first failed
		// subtask so the user lands on the retry target in one keystroke.
		if (this.opts.cursorOnFailed) {
			const idx = this.flatItems.findIndex((it) => it.subtask.status === "failed");
			if (idx >= 0) this.cursorIdx = idx;
			this.clampScroll();
		}
	}

	private rebuildItems(force = false): void {
		const tasks = this.opts.tasks();
		const count = tasks.reduce((n, t) => n + t.subtasks.length, 0);
		if (!force && count === this.lastSubtaskCount) return;
		this.lastSubtaskCount = count;
		this.flatItems = [];
		for (const task of tasks) {
			for (const st of task.subtasks) {
				this.flatItems.push({ taskId: task.id, subtask: st, depth: 0 });
			}
		}
		if (this.cursorIdx >= this.flatItems.length) {
			this.cursorIdx = Math.max(0, this.flatItems.length - 1);
			this.clampScroll();
		}
	}

	private currentItems(): { taskId: string; subtask: SubtaskResult; depth: number }[] {
		// ponytail: single source of truth for the visible list — used by BOTH
		// render (visible slice + footer count) AND handleInput (cursor bounds,
		// retry target, expand target) so they always agree on row count.
		if (!this.query) return this.flatItems;
		const q = this.query.toLowerCase();
		// ponytail: F4 — match status too (task-list overlay already does). `/ failed`
		// is the most common filter intent; without this it returns no match despite
		// failed subtasks existing.
		return this.flatItems.filter(
			(it) =>
				it.subtask.id.toLowerCase().includes(q) ||
				it.subtask.description.toLowerCase().includes(q) ||
				it.subtask.status.toLowerCase().includes(q),
		);
	}

	// ponytail: F9 — rows an item occupies when rendered: base line always,
	// plus error/result + meta lines when expanded. Meta-line PRESENCE is
	// width-independent (the width guard drops tags but keeps ≥1), so this
	// count is exact for windowing.
	private itemLineCount(item: { subtask: SubtaskResult }): number {
		if (!this.expanded.has(item.subtask.id)) return 1;
		let n = 1;
		if (item.subtask.error || item.subtask.result) n++;
		const hasMeta = (item.subtask.retryCount ?? 0) > 0
			|| !!item.subtask.review
			|| (!!item.subtask.dispatchMode && item.subtask.dispatchMode !== "prefer_remote");
		if (hasMeta) n++;
		return n;
	}

	// ponytail: F9 — does the target item still start inside the row budget when
	// the window begins at scrollOffset? Mirrors render's window loop (first item
	// always admitted). Used by clampScroll instead of the old item-count math,
	// which assumed 1 item = 1 row and let expanded items overflow the clamp.
	private fitsInWindow(target: number, items: { subtask: SubtaskResult }[]): boolean {
		let used = 0;
		for (let i = this.scrollOffset; i <= target && i < items.length; i++) {
			const n = this.itemLineCount(items[i]);
			if (used + n > this.maxVisible && i > this.scrollOffset) return false;
			used += n;
		}
		return true;
	}

	private clampScroll(): void {
		const items = this.currentItems();
		if (this.cursorIdx < this.scrollOffset) this.scrollOffset = this.cursorIdx;
		else {
			// ponytail: F9 — advance until the cursor item fits the row budget.
			// Cursor moves ≤1 item per keypress, so this iterates only a few times.
			while (this.scrollOffset < this.cursorIdx && !this.fitsInWindow(this.cursorIdx, items)) {
				this.scrollOffset++;
			}
		}
		if (this.scrollOffset < 0) this.scrollOffset = 0;
		// ponytail: clamp cursor into filtered bounds — query changes can shrink
		// the list below the current cursor, so snap it back.
		if (this.cursorIdx >= items.length) {
			this.cursorIdx = Math.max(0, items.length - 1);
		}
	}

	// ponytail: S5 — narrow-screen hint. The full hint line is ~73 chars; when the
	// compositor ANSI-truncates to a narrow terminal, the right side (Esc close,
	// / filter) is lost and the user can't see how to close. Use a compact version
	// that keeps the essential keys (nav, Enter, retry, Esc close) under 60
	// columns. Only applied to the normal (non-search, non-filtering) hint —
	// searchMode/filtering lines are short or user-typed.
	private hintLine(width: number, full: string, compact: string): string {
		return width < 60 ? compact : full;
	}

	render(width: number): string[] {
		this.rebuildItems();
		const lines: string[] = [];
		const tasks = this.opts.tasks();
		const items = this.currentItems();
		const filtering = this.query.length > 0;

		const headerExtra = filtering
			? ` — ${tasks.length} task(s), ${items.length} subtask(s) (filtered from ${this.flatItems.length})`
			: ` — ${tasks.length} task(s), ${this.flatItems.length} subtask(s)`;
		lines.push(this.theme.fg("accent", "  UC Subtask Tree") + this.theme.fg("dim", headerExtra));

		// ponytail: filter input line replaces the hint when searchMode or filter
		// active. Editing shows a cursor block; filter-active-not-editing shows
		// a mini hint for how to edit / clear. Normal hint adds `/ filter`.
		if (this.searchMode) {
			lines.push(this.theme.fg("dim", "  / ") + this.query + this.theme.bold("▏"));
		} else if (filtering) {
			lines.push(this.theme.fg("dim", `  filter: "${this.query}" — / to edit · Esc to clear`));
		} else {
			lines.push(this.theme.fg("dim", this.hintLine(width,
				"  ↑↓/jk nav · Enter detail · R retry · d task detail · PgUp/PgDn · g/G · / filter · Esc close",
				"  ↑↓ nav · Enter · R retry · Esc close",
			)));
		}
		lines.push("");

		if (this.flatItems.length === 0) {
			lines.push(this.theme.fg("dim", "  No tasks"));
			return lines;
		}

		// ponytail: empty filtered result — dim "no match" line so the user sees
		// feedback rather than a blank list.
		if (items.length === 0 && filtering) {
			lines.push(this.theme.fg("dim", `  no match for '${this.query}'`));
			if (this.flashMsg) {
				lines.push(this.theme.fg("dim", `  ${this.flashMsg.slice(0, Math.max(0, width - 2))}`));
			}
			return lines;
		}

		// ponytail: F9 — row-budget window, not item-count. Expanded items render
		// up to 3 lines; slicing by item count let total rows reach ~3× maxVisible,
		// overflowing the maxHeight clamp which silently cut the footer + flashMsg
		// (the exact failure overlay-pagination was written to prevent). The first
		// item is always admitted (≤3 rows ≪ budget) so the window never empties.
		const visible: typeof items = [];
		let usedLines = 0;
		let endIdx = this.scrollOffset;
		for (let i = this.scrollOffset; i < items.length; i++) {
			const n = this.itemLineCount(items[i]);
			if (usedLines + n > this.maxVisible && visible.length > 0) break;
			visible.push(items[i]);
			usedLines += n;
			endIdx = i + 1;
		}
		for (let i = 0; i < visible.length; i++) {
			const item = visible[i];
			const globalIdx = this.scrollOffset + i;
			const isCursor = globalIdx === this.cursorIdx;
			const cursor = isCursor ? this.theme.bold("›") : " ";
			const icon = statusIcon(item.subtask.status, this.theme);
			const desc = item.subtask.description.slice(0, Math.max(0, width - 16));
			const deps = item.subtask.dependsOn.length > 0
				? this.theme.fg("dim", ` ←${item.subtask.dependsOn.join(",")}`)
				: "";
			// ponytail: F8 — live elapsed on running rows (re-rendered by F7 timer).
			const elapsed = item.subtask.status === "running" && item.subtask.startedAt
				? this.theme.fg("dim", ` (${formatElapsed(Date.now() - item.subtask.startedAt)})`)
				: "";

			lines.push(`  ${cursor} ${icon} ${item.subtask.id}: ${desc}${deps}${elapsed}`);

			if (this.expanded.has(item.subtask.id)) {
				// ponytail: up to 2 detail lines per expanded subtask — line 1 is
				// the error (or result if no error) on its own line with full
				// width budget; line 2 is the secondary meta tags (review,
				// retry×N, dispatchMode) joined with " · ". Bounding detail to
				// ≤2 lines keeps itemLineCount's windowing exact (F9) — the old
				// code emitted up to 9 lines, overflowing the fixed overlay
				// height with no scroll.
				//
				// ponytail: error-on-own-line — the error is diagnostic-critical;
				// the old code joined error + review + retry + mode into ONE line
				// with " · ", so after the error consumed width-12, the appended
				// tags pushed the joined line past `width` and the compositor
				// ANSI-truncated the RIGHT side — dropping retry×N / dispatchMode
				// / review. Separating error onto its own line (width-8 budget)
				// ensures the error is never truncated by the tags, AND the meta
				// tags get their own short line with a width guard that drops
				// low-priority tags (dispatchMode first, then review) to keep
				// retry×N visible on narrow terminals.
				if (item.subtask.error) {
					lines.push("      " + formatErrorForDisplay(item.subtask.error, Math.max(0, width - 8), (c, t) => this.theme.fg(c, t)));
				} else if (item.subtask.result) {
					lines.push("      " + this.theme.fg("dim", item.subtask.result.split("\n")[0].slice(0, Math.max(0, width - 8))));
				}

				// ponytail: meta line — secondary tags joined with " · ". Track
				// plain lengths manually (NO pi-tui value imports — ANSI width
				// utils crash at runtime). Order by drop-priority: retry×N
				// (HIGH, shows retry state) > review (✓/✗) > dispatchMode (LOW,
				// rarely non-default). If the joined plain length exceeds the
				// budget (6 indent), drop dispatchMode first, then review.
				const metaParts: string[] = [];
				const metaPlain: string[] = [];
				if (item.subtask.retryCount && item.subtask.retryCount > 0) {
					const tag = `retry×${item.subtask.retryCount}`;
					metaParts.push(this.theme.fg("dim", tag));
					metaPlain.push(tag);
				}
				if (item.subtask.review) {
					const tag = item.subtask.review.approved ? "✓ approved" : "✗ rejected";
					metaParts.push(this.theme.fg("dim", tag));
					metaPlain.push(tag);
				}
				if (item.subtask.dispatchMode && item.subtask.dispatchMode !== "prefer_remote") {
					const tag = item.subtask.dispatchMode;
					metaParts.push(this.theme.fg("dim", tag));
					metaPlain.push(tag);
				}
				// ponytail: width guard — join plain strings with " · " and check
				// total plain length against width-6 (6 indent). If over budget,
				// drop the LAST tag (lowest priority = dispatchMode, then review)
				// and re-check until it fits. retry×N is always kept (index 0).
				const budget = Math.max(0, width - 6);
				while (metaPlain.length > 1 && metaPlain.join(" · ").length > budget) {
					metaPlain.pop();
					metaParts.pop();
				}
				if (metaParts.length > 0) {
					lines.push(`      ${metaParts.join(" · ")}`);
				}
			}
		}

		// ponytail: F9 — show the footer whenever rows are clipped (either side),
		// not when item count > maxVisible: few items all expanded still overflow.
		// Range reflects the actually-rendered window (endIdx), and ▲/▼ (F6) mark
		// the clipped side.
		if (this.scrollOffset > 0 || endIdx < items.length) {
			const up = this.scrollOffset > 0 ? "▲ " : "";
			const down = endIdx < items.length ? " ▼" : "";
			lines.push(this.theme.fg("dim", `  ${up}${this.scrollOffset + 1}-${endIdx} of ${items.length}${down}`));
		}

		// ponytail: flashMsg hint rendered after footer so it doesn't shift list rows.
		// Only present when set (non-null), so existing line-count assertions hold.
		if (this.flashMsg) {
			lines.push(this.theme.fg("dim", `  ${this.flashMsg.slice(0, Math.max(0, width - 2))}`));
		}

		return lines;
	}

	handleInput(data: string): void {
		// ponytail: clear flashMsg on any key that isn't r/R so navigation,
		// enter, esc, etc. dismiss the hint. Don't return — let the key
		// still do its normal thing. r/R refreshes the message instead.
		// In searchMode the flashMsg clear is skipped (filter editing doesn't
		// count as a "real" key for dismissal; the filter line is the focus).
		if (this.flashMsg && !this.searchMode && data !== "r" && data !== "R") {
			this.flashMsg = null;
		}

		// ── filter-editing mode: intercept printable/backspace/esc/enter ──────
		if (this.searchMode) {
			if (data === KEY.esc) {
				// Esc in search mode: clear query + exit (full list restored)
				this.query = "";
				this.searchMode = false;
				this.cursorIdx = 0;
				this.scrollOffset = 0;
				(this.tui as any)?.requestRender?.();
				return;
			}
			if (data === KEY.enter || data === "\n") {
				// Enter: exit editing but KEEP the filter active for navigation
				this.searchMode = false;
				this.clampScroll();
				(this.tui as any)?.requestRender?.();
				return;
			}
			if (data === "\x7f" || data === "\b") {
				// Backspace: drop last char; stay in filter mode even if empty
				this.query = this.query.slice(0, -1);
				this.clampScroll();
				(this.tui as any)?.requestRender?.();
				return;
			}
			// ponytail: printable single char (ASCII 0x20..0x7e, includes `/` itself)
			if (data.length === 1 && data >= " " && data <= "~") {
				this.query += data;
				this.clampScroll();
				(this.tui as any)?.requestRender?.();
				return;
			}
			// ponytail: nav keys (arrows/page/home/end/g/G/j/k) in search-edit:
			// exit editing (keep filter) then FALL THROUGH to normal handler so
			// the cursor moves within the filtered set in one keystroke.
			const navKeys = [KEY.up, KEY.down, KEY.pageUp, KEY.pageDown, KEY.home, KEY.end, "g", "G", "j", "k"];
			if (navKeys.includes(data)) {
				this.searchMode = false;
				// fall through to normal handling below
			} else if (data === "r" || data === "R") {
				// r/R in search-edit: exit editing (keep filter) then fall through
				this.searchMode = false;
				// fall through to normal handling below
			} else {
				// Unknown control sequence in search mode — ignore (don't exit)
				(this.tui as any)?.requestRender?.();
				return;
			}
		}

		// ── normal (non-search) mode ─────────────────────────────────────────
		if (data === "/") {
			// Enter filter mode (or resume editing an existing filter)
			this.searchMode = true;
			// ponytail: clear flashMsg when entering filter — `/` is a non-r/R key
			this.flashMsg = null;
			(this.tui as any)?.requestRender?.();
			return;
		}

		if (data === KEY.esc || data === "q") {
			// ponytail: if a filter is active, Esc clears it first (stay open);
			// only a second Esc (or Esc with no filter) closes the overlay.
			if (this.query) {
				this.query = "";
				this.cursorIdx = 0;
				this.scrollOffset = 0;
				(this.tui as any)?.requestRender?.();
				return;
			}
			this.done();
			return;
		}

		const items = this.currentItems();
		if (data === KEY.up || data === "k") {
			if (this.cursorIdx > 0) this.cursorIdx--;
		} else if (data === KEY.down || data === "j") {
			if (this.cursorIdx < items.length - 1) this.cursorIdx++;
		} else if (data === KEY.pageUp) {
			this.cursorIdx = Math.max(0, this.cursorIdx - this.maxVisible);
		} else if (data === KEY.pageDown) {
			// ponytail: Math.max(0, …) — empty list makes items.length-1 = -1,
			// which would clamp cursorIdx to -1 and render a phantom cursor.
			this.cursorIdx = Math.max(0, Math.min(items.length - 1, this.cursorIdx + this.maxVisible));
		} else if (data === KEY.home) {
			this.cursorIdx = 0;
		} else if (data === KEY.end) {
			this.cursorIdx = Math.max(0, items.length - 1);
		} else if (data === "g") {
			this.cursorIdx = 0;
		} else if (data === "G") {
			this.cursorIdx = Math.max(0, items.length - 1);
		} else if (data === KEY.enter || data === "\n") {
			// ponytail: S6 — Enter on an empty list / no cursor sets a flashMsg so the
			// user knows the keystroke was received, instead of silent no-op.
			const item = items[this.cursorIdx];
			if (item) {
				if (this.expanded.has(item.subtask.id)) {
					this.expanded.delete(item.subtask.id);
				} else {
					this.expanded.add(item.subtask.id);
				}
			} else {
				this.flashMsg = "no subtask selected";
			}
		} else if (data === "r" || data === "R") {
			const item = items[this.cursorIdx];
			if (item && item.subtask.status === "failed" && this.opts.onRetry) {
				this.opts.onRetry(item.taskId, item.subtask.id);
				this.flashMsg = null;
			} else {
				// ponytail: dead-key feedback — r on a non-failed subtask (or no
				// cursor / no onRetry) sets a dim hint naming the actual status
				// so the user knows why nothing happened.
				const status = item ? item.subtask.status : "no subtask selected";
				this.flashMsg = `only failed subtasks can be retried (cursor is ${status})`;
			}
		} else if (data === "d") {
			// ponytail: jump to parent task detail — open the task-list overlay on
			// this subtask's task (close the tree first so overlays don't stack).
			// S6: restructure so no-item case gets feedback (was silent on empty list).
			const item = items[this.cursorIdx];
			if (!item) {
				this.flashMsg = "no subtask selected";
			} else if (!this.opts.onJumpToTask) {
				this.flashMsg = "jump unavailable";
			} else {
				this.opts.onJumpToTask(item.taskId);
				this.done();
			}
		}
		this.clampScroll();
		// ponytail: requestRender on tui — type assertion needed since TUI type is opaque.
		// Guard for undefined tui (selfcheck passes undefined as the mock).
		(this.tui as any)?.requestRender?.();
	}

	invalidate(): void {
		this.rebuildItems(true);
	}

	dispose(): void {
		if (this.refreshTimer) {
			clearInterval(this.refreshTimer);
			this.refreshTimer = null;
		}
	}
}
