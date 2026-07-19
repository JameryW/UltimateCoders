/**
 * Shared status icons for UC UI components.
 *
 * Single source of truth for status→icon+color mapping used by the progress
 * widget, subtask tree, task result renderer, and status formatter. Previously
 * duplicated 4× with subtle drift (task-result-renderer was missing `planning`).
 *
 * import type only — this module carries no runtime pi-tui value imports.
 */

import type { Theme } from "@oh-my-pi/pi-coding-agent";

// ponytail: superset of task + subtask status unions; unknown statuses render as
// "?" (F5 — previously fell back to pending ○, silently mislabeling new/renamed
// statuses from a newer server as "not started").
// `in_progress` is a task-level status (task-list overlay uses STATUS_BADGE "run";
// this icon mirrors it for /uc status + task-result-renderer so the same task
// doesn't render as ○ pending there).
export const STATUS_ICON: Record<string, (theme: Theme) => string> = {
	completed: (t) => t.fg("success", "✓"),
	running: (t) => t.fg("warning", "●"),
	reviewing: (t) => t.fg("accent", "◉"),
	failed: (t) => t.fg("error", "✗"),
	cancelled: (t) => t.fg("dim", "⊘"),
	pending: (t) => t.fg("dim", "○"),
	planning: (t) => t.fg("dim", "◎"),
	in_progress: (t) => t.fg("warning", "●"),
	unknown: (t) => t.fg("dim", "?"),
};

export function statusIcon(status: string, theme: Theme): string {
	return (STATUS_ICON[status] ?? STATUS_ICON.unknown)(theme);
}
