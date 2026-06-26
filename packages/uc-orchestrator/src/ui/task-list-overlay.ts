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
	onSelect?: (taskId: string) => void;
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

	constructor(
		private opts: TaskListOptions,
		private tui: TUI,
		private theme: Theme,
		private done: (result: void) => void,
	) {}

	render(width: number): string[] {
		const tasks = this.opts.tasks();
		const lines: string[] = [];

		lines.push(this.theme.fg("accent", "  UC Tasks") + this.theme.fg("dim", ` — ${tasks.length} task(s)`));
		lines.push(this.theme.fg("dim", "  ↑↓ navigate · Enter select · Esc close"));
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
			const desc = task.description.slice(0, width - 30);
			const age = this.formatAge(task.createdAt);

			lines.push(`  ${cursor} ${badge} ${task.id.slice(0, 14)} ${completed}/${total} ${desc}`);
			lines.push(this.theme.fg("dim", `      ${age}`));
		}

		if (tasks.length > this.maxVisible) {
			lines.push(this.theme.fg("dim", `  ${this.scrollOffset + 1}-${Math.min(this.scrollOffset + this.maxVisible, tasks.length)} of ${tasks.length}`));
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
		const tasks = this.opts.tasks();

		if (data === "\x1b" || data === "q") {
			this.done();
			return;
		}
		if (data === "\x1b[A" || data === "k") {
			if (this.cursorIdx > 0) {
				this.cursorIdx--;
				if (this.cursorIdx < this.scrollOffset) this.scrollOffset = this.cursorIdx;
			}
		} else if (data === "\x1b[B" || data === "j") {
			if (this.cursorIdx < tasks.length - 1) {
				this.cursorIdx++;
				if (this.cursorIdx >= this.scrollOffset + this.maxVisible) this.scrollOffset++;
			}
		} else if (data === "\r" || data === "\n") {
			const task = tasks[this.cursorIdx];
			if (task && this.opts.onSelect) {
				this.opts.onSelect(task.id);
			}
		}
		(this.tui as any).requestRender?.();
	}

	invalidate(): void {}
	dispose(): void {}
}
