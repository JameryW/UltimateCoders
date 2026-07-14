/**
 * TaskListOverlay — interactive task list overlay for OMP TUI.
 *
 * Opens via Ctrl+Shift+T shortcut. Shows all tasks with status,
 * subtask counts, timestamps. Navigate with keyboard, Enter for detail.
 *
 * Uses: ui.custom(listFactory, { overlay: true })
 */

import type { Component, TUI } from "@oh-my-pi/pi-tui";
import type { Theme } from "@oh-my-pi/pi-coding-agent";
import type { TaskState } from "../orchestrator/orchestrator";
import { formatTaskDetail } from "./status-formatter";

// ponytail: raw xterm key sequences — pi-tui value imports crash at runtime
// (vendor utils setNativeKillTree mismatch), match bytes directly.
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

// ── Status badge ─────────────────────────────────────────────────

const STATUS_BADGE: Record<string, (theme: Theme) => string> = {
	completed: (t) => t.fg("success", "done"),
	failed: (t) => t.fg("error", "fail"),
	cancelled: (t) => t.fg("dim", "stop"),
	in_progress: (t) => t.fg("warning", "run "),
	planning: (t) => t.fg("dim", "plan"),
	paused: (t) => t.fg("accent", "hold"),
};

function statusBadge(status: string, theme: Theme): string {
	return (STATUS_BADGE[status] ?? STATUS_BADGE.planning)(theme);
}

// ── TaskList Component ───────────────────────────────────────────

export interface TaskListOptions {
	tasks: () => TaskState[];
	// ponytail: detail rendered in-overlay (Esc returns) instead of notify() spam
	getTask?: (taskId: string) => TaskState | undefined;
	onClose: () => void;
}

export function createTaskListOverlay(opts: TaskListOptions) {
	return (tui: any, theme: Theme, _keybindings: unknown, done: (result: void) => void): Component & { dispose?(): void } => {
		return new TaskListComponent(opts, tui, theme, done);
	};
}

class TaskListComponent {
	private cursorIdx = 0;
	private scrollOffset = 0;
	private maxVisible = 20;
	// detail mode: showing one task's breakdown, Esc returns to list
	private detailTaskId: string | null = null;
	private detailLines: string[] = [];
	private detailScroll = 0;

	constructor(
		private opts: TaskListOptions,
		private tui: TUI,
		private theme: Theme,
		private done: (result: void) => void,
	) {}

	render(width: number): string[] {
		if (this.detailTaskId) return this.renderDetail(width);
		return this.renderList(width);
	}

	private renderList(width: number): string[] {
		const tasks = this.opts.tasks();
		const lines: string[] = [];

		lines.push(this.theme.fg("accent", "  UC Tasks") + this.theme.fg("dim", ` — ${tasks.length} task(s)`));
		lines.push(this.theme.fg("dim", "  ↑↓/jk navigate · Enter detail · PgUp/PgDn · g/G · Esc close"));
		lines.push("");

		if (tasks.length === 0) {
			lines.push(this.theme.fg("dim", "  No tasks"));
			return lines;
		}

		const visible = tasks.slice(this.scrollOffset, this.scrollOffset + this.maxVisible);
		for (let i = 0; i < visible.length; i++) {
			const task = visible[i];
			const globalIdx = this.scrollOffset + i;
			const isCursor = globalIdx === this.cursorIdx;
			const cursor = isCursor ? this.theme.bold("›") : " ";
			const badge = statusBadge(task.status, this.theme);
			const completed = task.subtasks.filter((s) => s.status === "completed").length;
			const total = task.subtasks.length;
			const age = this.formatAge(task.createdAt);
			// ponytail: one line per task — age folded into a dim suffix so the
			// visible window (maxVisible tasks) matches actual rendered rows.
			// Previously the 2nd age line doubled the row count, pushing rows
			// past the overlay height while the footer still claimed 1-N fit.
			const ageSuffix = this.theme.fg("dim", ` ${age}`);
			const desc = task.description.slice(0, Math.max(0, width - 34 - age.length));

			lines.push(`  ${cursor} ${badge} ${task.id.slice(0, 14)} ${completed}/${total} ${desc}${ageSuffix}`);
		}

		if (tasks.length > this.maxVisible) {
			lines.push(this.theme.fg("dim", `  ${this.scrollOffset + 1}-${Math.min(this.scrollOffset + this.maxVisible, tasks.length)} of ${tasks.length}`));
		}

		return lines;
	}

	private renderDetail(width: number): string[] {
		const lines: string[] = [];
		lines.push(this.theme.fg("accent", `  Task ${this.detailTaskId?.slice(0, 14) ?? ""}`));
		lines.push(this.theme.fg("dim", "  ↑↓/jk scroll · Esc back to list"));
		lines.push("");
		const maxVisible = this.maxVisible;
		const start = this.detailScroll;
		const slice = this.detailLines.slice(start, start + maxVisible);
		for (const l of slice) {
			lines.push(`  ${l.slice(0, Math.max(0, width - 2))}`);
		}
		if (this.detailLines.length > maxVisible) {
			lines.push(this.theme.fg("dim", `  ${start + 1}-${Math.min(start + maxVisible, this.detailLines.length)} of ${this.detailLines.length}`));
		}
		return lines;
	}

	private formatAge(ts: number): string {
		const diff = Date.now() - ts;
		if (diff < 60_000) return `${Math.floor(diff / 1000)}s ago`;
		if (diff < 3600_000) return `${Math.floor(diff / 60_000)}m ago`;
		if (diff < 86400_000) return `${Math.floor(diff / 3600_000)}h ago`;
		return `${Math.floor(diff / 86400_000)}d ago`;
	}

	handleInput(data: string): void {
		if (this.detailTaskId) {
			// detail scroll mode
			if (data === KEY.esc) {
				this.detailTaskId = null;
				this.detailScroll = 0;
				return;
			}
			if (data === KEY.up || data === "k") this.detailScroll = Math.max(0, this.detailScroll - 1);
			else if (data === KEY.down || data === "j") this.detailScroll = Math.min(Math.max(0, this.detailLines.length - this.maxVisible), this.detailScroll + 1);
			else if (data === KEY.pageUp) this.detailScroll = Math.max(0, this.detailScroll - this.maxVisible);
			else if (data === KEY.pageDown) this.detailScroll = Math.min(Math.max(0, this.detailLines.length - this.maxVisible), this.detailScroll + this.maxVisible);
			else if (data === KEY.home || data === "g") this.detailScroll = 0;
			else if (data === KEY.end || data === "G") this.detailScroll = Math.max(0, this.detailLines.length - this.maxVisible);
			return;
		}

		const tasks = this.opts.tasks();

		if (data === KEY.esc || data === "q") {
			this.done();
			return;
		}
		if (data === KEY.up || data === "k") {
			if (this.cursorIdx > 0) this.cursorIdx--;
		} else if (data === KEY.down || data === "j") {
			if (this.cursorIdx < tasks.length - 1) this.cursorIdx++;
		} else if (data === KEY.pageUp) {
			this.cursorIdx = Math.max(0, this.cursorIdx - this.maxVisible);
		} else if (data === KEY.pageDown) {
			// ponytail: Math.max(0, …) — empty list makes tasks.length-1 = -1,
			// which would clamp cursorIdx to -1 and render a phantom cursor.
			this.cursorIdx = Math.max(0, Math.min(tasks.length - 1, this.cursorIdx + this.maxVisible));
		} else if (data === KEY.home) {
			this.cursorIdx = 0;
		} else if (data === KEY.end) {
			this.cursorIdx = Math.max(0, tasks.length - 1);
		} else if (data === "g") {
			this.cursorIdx = 0;
		} else if (data === "G") {
			this.cursorIdx = Math.max(0, tasks.length - 1);
		} else if (data === KEY.enter || data === "\n") {
			const task = tasks[this.cursorIdx];
			if (task) this.openDetail(task.id);
		}
		// clamp scroll to cursor
		if (this.cursorIdx < this.scrollOffset) this.scrollOffset = this.cursorIdx;
		else if (this.cursorIdx >= this.scrollOffset + this.maxVisible) {
			this.scrollOffset = this.cursorIdx - this.maxVisible + 1;
		}
		if (this.scrollOffset < 0) this.scrollOffset = 0;
	}

	private openDetail(taskId: string): void {
		const task = this.opts.getTask ? this.opts.getTask(taskId) : this.opts.tasks().find((t) => t.id === taskId);
		if (!task) return;
		this.detailTaskId = taskId;
		this.detailLines = formatTaskDetail(task, this.theme);
		this.detailScroll = 0;
	}

	invalidate(): void {}
	dispose(): void {}
}
