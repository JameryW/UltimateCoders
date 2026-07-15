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

	constructor(
		private opts: SubtaskTreeOptions,
		private tui: TUI,
		private theme: Theme,
		private done: (result: void) => void,
	) {
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
		return this.flatItems.filter(
			(it) =>
				it.subtask.id.toLowerCase().includes(q) ||
				it.subtask.description.toLowerCase().includes(q),
		);
	}

	private clampScroll(): void {
		const items = this.currentItems();
		if (this.cursorIdx < this.scrollOffset) this.scrollOffset = this.cursorIdx;
		else if (this.cursorIdx >= this.scrollOffset + this.maxVisible) {
			this.scrollOffset = this.cursorIdx - this.maxVisible + 1;
		}
		if (this.scrollOffset < 0) this.scrollOffset = 0;
		// ponytail: clamp cursor into filtered bounds — query changes can shrink
		// the list below the current cursor, so snap it back.
		if (this.cursorIdx >= items.length) {
			this.cursorIdx = Math.max(0, items.length - 1);
		}
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
			lines.push(this.theme.fg("dim", "  ↑↓/jk nav · Enter detail · R retry · d task detail · PgUp/PgDn · g/G · / filter · Esc close"));
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

		const visible = items.slice(this.scrollOffset, this.scrollOffset + this.maxVisible);
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

			lines.push(`  ${cursor} ${icon} ${item.subtask.id}: ${desc}${deps}`);

			if (this.expanded.has(item.subtask.id)) {
				// ponytail: one detail line per expanded subtask. The old code emitted
				// up to 9 lines (5 result + error + review + retries + mode), which made
				// maxVisible (paginated by subtask count) undercount rendered rows —
				// expanded detail overflowed the fixed overlay height with no scroll.
				// Single preview line keeps rows 1:1 with flatItems for sane paging.
				const parts: string[] = [];
				if (item.subtask.error) {
					parts.push(formatErrorForDisplay(item.subtask.error, Math.max(0, width - 12), (c, t) => this.theme.fg(c, t)));
				} else if (item.subtask.result) {
					parts.push(this.theme.fg("dim", item.subtask.result.split("\n")[0].slice(0, Math.max(0, width - 8))));
				}
				if (item.subtask.review) {
					parts.push(this.theme.fg("dim", item.subtask.review.approved ? "✓ approved" : "✗ rejected"));
				}
				if (item.subtask.retryCount && item.subtask.retryCount > 0) {
					parts.push(this.theme.fg("dim", `retry×${item.subtask.retryCount}`));
				}
				if (item.subtask.dispatchMode && item.subtask.dispatchMode !== "prefer_remote") {
					parts.push(this.theme.fg("dim", item.subtask.dispatchMode));
				}
				if (parts.length > 0) {
					lines.push(`      ${parts.join(" · ")}`);
				}
			}
		}

		if (items.length > this.maxVisible) {
			lines.push(this.theme.fg("dim", `  ${this.scrollOffset + 1}-${Math.min(this.scrollOffset + this.maxVisible, items.length)} of ${items.length}`));
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
			const item = items[this.cursorIdx];
			if (item) {
				if (this.expanded.has(item.subtask.id)) {
					this.expanded.delete(item.subtask.id);
				} else {
					this.expanded.add(item.subtask.id);
				}
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
			const item = items[this.cursorIdx];
			if (item && this.opts.onJumpToTask) {
				this.opts.onJumpToTask(item.taskId);
				this.done();
			} else if (!this.opts.onJumpToTask) {
				this.flashMsg = "jump unavailable";
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

	dispose(): void {}
}
