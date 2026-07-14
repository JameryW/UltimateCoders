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
	private maxVisible = 20; // ponytail: reasonable default
	// ponytail: stamp flatItems only when task count changes, not every render
	private lastSubtaskCount = -1;

	constructor(
		private opts: SubtaskTreeOptions,
		private tui: TUI,
		private theme: Theme,
		private done: (result: void) => void,
	) {
		this.rebuildItems();
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

	private clampScroll(): void {
		if (this.cursorIdx < this.scrollOffset) this.scrollOffset = this.cursorIdx;
		else if (this.cursorIdx >= this.scrollOffset + this.maxVisible) {
			this.scrollOffset = this.cursorIdx - this.maxVisible + 1;
		}
		if (this.scrollOffset < 0) this.scrollOffset = 0;
	}

	render(width: number): string[] {
		this.rebuildItems();
		const lines: string[] = [];
		const tasks = this.opts.tasks();

		lines.push(this.theme.fg("accent", "  UC Subtask Tree") + this.theme.fg("dim", ` — ${tasks.length} task(s), ${this.flatItems.length} subtask(s)`));
		lines.push(this.theme.fg("dim", "  ↑↓/jk nav · Enter detail · R retry · PgUp/PgDn · g/G · Esc close"));
		lines.push("");

		if (this.flatItems.length === 0) {
			lines.push(this.theme.fg("dim", "  No tasks"));
			return lines;
		}

		const visible = this.flatItems.slice(this.scrollOffset, this.scrollOffset + this.maxVisible);
		for (let i = 0; i < visible.length; i++) {
			const item = visible[i];
			const globalIdx = this.scrollOffset + i;
			const isCursor = globalIdx === this.cursorIdx;
			const cursor = isCursor ? this.theme.bold("›") : " ";
			const icon = statusIcon(item.subtask.status, this.theme);
			const desc = item.subtask.description.slice(0, width - 16);
			const deps = item.subtask.dependsOn.length > 0
				? this.theme.fg("dim", ` ←${item.subtask.dependsOn.join(",")}`)
				: "";

			lines.push(`  ${cursor} ${icon} ${item.subtask.id}: ${desc}${deps}`);

			if (this.expanded.has(item.subtask.id)) {
				if (item.subtask.result) {
					const resultLines = item.subtask.result.split("\n").slice(0, 5);
					for (const rl of resultLines) {
						lines.push(this.theme.fg("dim", `      ${rl.slice(0, width - 8)}`));
					}
				}
				if (item.subtask.error) {
					lines.push(`      ${formatErrorForDisplay(item.subtask.error, width - 12, (c, t) => this.theme.fg(c, t))}`);
				}
				if (item.subtask.review) {
					const approved = item.subtask.review.approved ? "approved" : "rejected";
					lines.push(this.theme.fg("dim", `      Review: ${approved}`));
				}
				if (item.subtask.retryCount && item.subtask.retryCount > 0) {
					lines.push(this.theme.fg("dim", `      Retries: ${item.subtask.retryCount}`));
				}
				if (item.subtask.dispatchMode && item.subtask.dispatchMode !== "prefer_remote") {
					lines.push(this.theme.fg("dim", `      Mode: ${item.subtask.dispatchMode}`));
				}
			}
		}

		if (this.flatItems.length > this.maxVisible) {
			lines.push(this.theme.fg("dim", `  ${this.scrollOffset + 1}-${Math.min(this.scrollOffset + this.maxVisible, this.flatItems.length)} of ${this.flatItems.length}`));
		}

		return lines;
	}

	handleInput(data: string): void {
		if (data === KEY.esc || data === "q") {
			this.done();
			return;
		}
		if (data === KEY.up || data === "k") {
			if (this.cursorIdx > 0) this.cursorIdx--;
		} else if (data === KEY.down || data === "j") {
			if (this.cursorIdx < this.flatItems.length - 1) this.cursorIdx++;
		} else if (data === KEY.pageUp) {
			this.cursorIdx = Math.max(0, this.cursorIdx - this.maxVisible);
		} else if (data === KEY.pageDown) {
			this.cursorIdx = Math.min(this.flatItems.length - 1, this.cursorIdx + this.maxVisible);
		} else if (data === KEY.home) {
			this.cursorIdx = 0;
		} else if (data === KEY.end) {
			this.cursorIdx = Math.max(0, this.flatItems.length - 1);
		} else if (data === "g") {
			this.cursorIdx = 0;
		} else if (data === "G") {
			this.cursorIdx = Math.max(0, this.flatItems.length - 1);
		} else if (data === KEY.enter || data === "\n") {
			const item = this.flatItems[this.cursorIdx];
			if (item) {
				if (this.expanded.has(item.subtask.id)) {
					this.expanded.delete(item.subtask.id);
				} else {
					this.expanded.add(item.subtask.id);
				}
			}
		} else if (data === "r" || data === "R") {
			const item = this.flatItems[this.cursorIdx];
			if (item && item.subtask.status === "failed" && this.opts.onRetry) {
				this.opts.onRetry(item.taskId, item.subtask.id);
			}
		}
		this.clampScroll();
		// ponytail: requestRender on tui — type assertion needed since TUI type is opaque
		(this.tui as any).requestRender?.();
	}

	invalidate(): void {
		this.rebuildItems(true);
	}

	dispose(): void {}
}
