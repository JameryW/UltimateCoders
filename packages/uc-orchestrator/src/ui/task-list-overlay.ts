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
import { overlayPageSize } from "./overlay-pagination";
import { copyText } from "./clipboard";

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
	// ponytail: F5 — unknown status shows "?" (4-wide like the other badges)
	// instead of masquerading as "plan". Happens when the server runs a newer
	// status the TUI doesn't know yet.
	unknown: (t) => t.fg("dim", "?   "),
};

function statusBadge(status: string, theme: Theme): string {
	return (STATUS_BADGE[status] ?? STATUS_BADGE.unknown)(theme);
}

// ── TaskList Component ───────────────────────────────────────────

export interface TaskListOptions {
	tasks: () => TaskState[];
	// ponytail: detail rendered in-overlay (Esc returns) instead of notify() spam
	getTask?: (taskId: string) => TaskState | undefined;
	/** Quick actions on the cursor task: c cancel / p pause / r resume.
	 * Returns true if the action applied (for in-overlay confirmation feedback). */
	onAction?: (taskId: string, action: "cancel" | "pause" | "resume") => boolean | Promise<boolean>;
	/** Open straight into detail mode for this task (jump-from-subtask-tree). */
	initialDetailTaskId?: string;
	/** `t` in detail mode opens the subtask-tree overlay on this task
	 * (the reverse of the tree's `d` → task detail jump). Undefined →
	 * `t` flashes "jump unavailable" (no tree opener wired). */
	onJumpToTree?: (taskId: string) => void;
	/** Clipboard writer for `y` yank (default: system clipboard). Injectable
	 * so selfchecks don't touch the real clipboard. */
	copy?: (text: string) => boolean;
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
	/**
	 * ponytail: height-adaptive page size (see overlayPageSize). Read live so a
	 * terminal resize takes effect on the next render/input without restart.
	 */
	private get maxVisible(): number {
		return overlayPageSize(this.tui);
	}
	// detail mode: showing one task's breakdown, Esc returns to list
	private detailTaskId: string | null = null;
	private detailLines: string[] = [];
	private detailScroll = 0;
	// ponytail: search/filter mode — `/` enters editing, typing narrows the list,
	// Enter/nav exits editing but keeps the filter active, Esc clears everything.
	// Applies to LIST mode only — detail mode is a single-task view, no list to filter.
	private searchMode = false;
	private query = "";
	// ponytail: cursor task id captured when filter editing starts, so clearing
	// the filter restores focus to the pre-filter cursor task (if still present)
	// instead of snapping back to row 0. Null = no capture / task gone.
	private preFilterCursorTaskId: string | null = null;
	// ponytail: double-tap cancel confirm — first `c` arms it (+ flashMsg),
	// second `c` fires onAction; any other key clears it (mirrors retry flashMsg).
	private pendingCancel: string | null = null;
	// ponytail: transient hint for dead/confirm keys (c on non-cancellable, etc).
	// Cleared on the next non-c keypress so nav/enter/esc dismisses it.
	private flashMsg: string | null = null;
	// ponytail: F7 — 1s tick so time-based fields (task ages) re-render while
	// the overlay sits open (formatAge froze at the last event-driven render).
	// Cleared in dispose(); modal overlays are short-lived.
	private refreshTimer: ReturnType<typeof setInterval> | null = null;

	constructor(
		private opts: TaskListOptions,
		private tui: TUI,
		private theme: Theme,
		private done: (result: void) => void,
	) {
		this.refreshTimer = setInterval(() => (this.tui as any)?.requestRender?.(), 1000);
		// ponytail: jump-from-subtask-tree lands directly in detail mode.
		if (this.opts.initialDetailTaskId) this.openDetail(this.opts.initialDetailTaskId);
	}

	// ponytail: single source of truth for the visible list — used by BOTH
	// renderList (visible slice + footer count + header) AND handleInput list-mode
	// (cursor bounds, openDetail target, nav) so they always agree on row count.
	// Detail mode does NOT use currentTasks — detail is a single-task view.
	private currentTasks(): TaskState[] {
		if (!this.query) return this.opts.tasks();
		const q = this.query.toLowerCase();
		// ponytail: match controlState too — a paused task keeps status="in_progress"
		// and only flips controlState="paused", so filtering "paused" (the row badge
		// the user sees) returned no matches despite paused tasks existing. Same for
		// a future "cancelled" controlState if it ever diverges from status.
		return this.opts.tasks().filter(
			(t) =>
				t.id.toLowerCase().includes(q) ||
				t.description.toLowerCase().includes(q) ||
				t.status.toLowerCase().includes(q) ||
				(t.controlState ?? "").toLowerCase().includes(q) ||
				(t.error ?? "").toLowerCase().includes(q) ||
				// ponytail: match any subtask status too — a task stays "in_progress"
				// while a subtask fails (the common triage state), so `/failed` matched
				// only failed tasks and hid the in_progress task with a failed subtask.
				// `/failed` now surfaces every task that HAS a failed subtask. Same for
				// `/running`, `/completed`, etc. .some() short-circuits on the first hit.
				// ponytail: match subtask error + result text too — mirrors the subtask-tree
				// filter (#447). `/timeout` surfaces an in_progress task whose failed subtask
				// mentions it, not just tasks with a task-level error.
				// ponytail: match subtask declared files too — typing a path (e.g. "foo.ts")
				// surfaces tasks with a subtask touching it. Mirrors the tree filter; the row
				// shows "N file(s)" (#484/#493) but the filter previously couldn't find them.
				t.subtasks.some((s) =>
					s.status.toLowerCase().includes(q) ||
					(s.error ?? "").toLowerCase().includes(q) ||
					(s.result ?? "").toLowerCase().includes(q) ||
					(s.files ?? []).some((f) => f.toLowerCase().includes(q))),
		);
	}

	// ponytail: S5 — narrow-screen hint. The full hint line is ~78 chars; when the
	// compositor ANSI-truncates to a narrow terminal, the right side (Esc close,
	// / filter) is lost and the user can't see how to close. Use a compact version
	// that keeps the essential keys (nav, Enter, quick actions, Esc close) under
	// 60 columns. Only applied to the normal (non-search, non-filtering) hint —
	// searchMode/filtering lines are short or user-typed.
	// ponytail: switch to the compact hint when the FULL hint won't fit (not a fixed
	// 60-col threshold). The full hint is ~100 chars; at the common 80-col terminal it
	// was returned anyway and the compositor right-truncated it — dropping `/ filter`
	// and `Esc close` (both load-bearing: the close path + the filter entry). Compact
	// keeps nav + actions + Esc visible. ANSI-stripped length check (plain strings,
	// no theme codes here — the hint is themed by the caller AFTER this returns).
	private hintLine(width: number, full: string, compact: string): string {
		return full.length + 2 <= width ? full : compact;
	}

	render(width: number): string[] {
		if (this.detailTaskId) return this.renderDetail(width);
		return this.renderList(width);
	}

	private renderList(width: number): string[] {
		const allTasks = this.opts.tasks();
		const tasks = this.currentTasks();
		// ponytail: re-clamp on render — a terminal height shrink (maxVisible drops) can
		// leave the cursor outside the scrollOffset..maxVisible window (it was last
		// clamped in handleInput, but render runs on the 1s tick + resize without an
		// input event). Without this, the cursor row left the viewport and the `›`
		// marker rendered nowhere — the user lost their place until the next keystroke.
		this.clampCursorAndScroll();
		const filtering = this.query.length > 0;
		const lines: string[] = [];

		// ponytail: failed-count marker in the header — mirrors the subtask-tree
		// header (#441). A task counts as "failed" if its status is failed OR a subtask
		// failed (matches the /failed filter semantics + the row ·N✗ marker, #429): an
		// in_progress task with a failed subtask is the common triage target. error-
		// colored so it reads as a warning. No marker when 0 failed (no noise).
		const failedCount = tasks.filter(
			(t) => this.isTaskFailed(t),
		).length;
		const failTag = failedCount > 0 ? this.theme.fg("error", ` ·${failedCount}✗`) : "";
		// ponytail: running-count marker — mirrors the subtask-tree header (#458).
		// A task counts as "running" if it's in_progress/planning/paused (a live
		// concern) — answers "is anything making progress" at a glance, without
		// scanning rows. accent-colored so a live run reads as active. No marker
		// when 0 running (no noise on an all-terminal list).
		const runningCount = tasks.filter(
			(t) => ["in_progress", "planning", "paused"].includes(t.status),
		).length;
		const runTag = runningCount > 0 ? this.theme.fg("accent", ` ·${runningCount}▶`) : "";
		// ponytail: blocked-count marker — mirrors the subtask-tree header (#486) +
		// detail countTag (#485) + notify row (#480). The header showed failed+running
		// but not how many tasks have blocked subtasks. dim "·N⏳" only when blocked>0.
		const blockedCount = tasks.filter((t) =>
			t.subtasks.some((s) =>
				s.status === "pending" && s.dependsOn.length > 0 &&
				s.dependsOn.some((depId) => {
					const dep = t.subtasks.find((d) => d.id === depId);
					return !dep || dep.status !== "completed";
				})
			)
		).length;
		const blockTag = blockedCount > 0 ? this.theme.fg("dim", ` ·${blockedCount}⏳`) : "";
		const headerExtra = filtering
			? ` — ${tasks.length} task(s) (filtered from ${allTasks.length})`
			: ` — ${allTasks.length} task(s)`;
		lines.push(this.theme.fg("accent", "  UC Tasks") + this.theme.fg("dim", headerExtra) + failTag + runTag + blockTag);

		// ponytail: filter input line replaces the hint when searchMode or filter
		// active. Editing shows a cursor block; filter-active-not-editing shows
		// a mini hint for how to edit / clear. Normal hint adds `/ filter`.
		if (this.searchMode) {
			lines.push(this.theme.fg("dim", "  / ") + this.query + this.theme.bold("▏"));
		} else if (filtering) {
			lines.push(this.theme.fg("dim", `  filter: "${this.query}" — / to edit · Esc to clear`));
		} else {
			lines.push(this.theme.fg("dim", this.hintLine(width,
				"  ↑↓/jk nav · Enter detail · c cancel (2×) · p pause · r resume · n next-failed · N prev-failed · t tree · PgUp/PgDn · y/Y copy · / filter · Esc close",
				"  ↑↓ nav · Enter · c/p/r · n/N failed · t tree · Esc close",
			)));
		}
		lines.push("");

		if (allTasks.length === 0) {
			lines.push(this.theme.fg("dim", "  No tasks"));
			// ponytail: a new user who opens the list (Ctrl+Shift+T) to an empty
			// store sees "No tasks" + the in-overlay key hint, but neither names
			// how tasks get IN. One dim line pointing at /uc submit (mirrors the
			// /uc status <task-id> hint in the status toast). Skipped when a filter
			// is active (filtering an empty list is a user-driven no-op, not a
			// first-run cue).
			lines.push(this.theme.fg("dim", "  /uc submit <description> to add one"));
			return lines;
		}

		// ponytail: empty filtered result — dim "no match" line so the user sees
		// feedback rather than a blank list.
		if (tasks.length === 0 && filtering) {
			lines.push(this.theme.fg("dim", `  no match for '${this.query}'`));
			return lines;
		}

		const visible = tasks.slice(this.scrollOffset, this.scrollOffset + this.maxVisible);
		for (let i = 0; i < visible.length; i++) {
			const task = visible[i];
			const globalIdx = this.scrollOffset + i;
			const isCursor = globalIdx === this.cursorIdx;
			const cursor = isCursor ? this.theme.bold("›") : " ";
			const badge = statusBadge(
				// ponytail: pause sets controlState="paused" but leaves status as
				// in_progress/planning — without this a paused task shows "run "
				// (running). cancelled already flips status, so only paused needs
				// the override. Mirrors formatTaskList's [controlState] suffix.
				task.controlState === "paused" ? "paused" : task.status,
				this.theme,
			);
			const completed = task.subtasks.filter((s) => s.status === "completed").length;
			const failed = task.subtasks.filter((s) => s.status === "failed").length;
			const total = task.subtasks.length;
			const age = this.taskAge(task);
			// ponytail: one line per task — age folded into a dim suffix so the
			// visible window (maxVisible tasks) matches actual rendered rows.
			// Previously the 2nd age line doubled the row count, pushing rows
			// past the overlay height while the footer still claimed 1-N fit.
			const ageSuffix = this.theme.fg("dim", ` ${age}`);
			// ponytail: failed-count marker — 3/5 with 2 failed read as "3/5", same as
			// 3 completed + 2 pending, so a stuck task hid its failures in the list.
			// Append " ·N✗" only when failed > 0 (no noise on healthy tasks). Mirrors
			// the progress bar's failed segment (#427). Built plain so its length feeds
			// the desc budget (prefixLen below).
			const failTag = failed > 0 ? ` ·${failed}✗` : "";
			// ponytail: running-count marker — mirrors the task-list header (#459) +
			// detail countTag (#461). The list row showed failed count but not how many
			// subtasks are actively running, so "is this making progress" needed opening
			// the detail. Append " ·N▶" only when running>0 (no noise on terminal tasks).
			const running = task.subtasks.filter((s) => s.status === "running" || s.status === "reviewing").length;
			const runTag = running > 0 ? ` ·${running}▶` : "";
			// ponytail: blocked-count marker — mirrors the notify row (#480) + detail
			// view's ⏳N (#473). A pending subtask w/ unmet deps is "blocked"; the list
			// row showed failed but not how many are stalled on deps. Only when blocked > 0.
			const subById = new Map(task.subtasks.map((s) => [s.id, s]));
			const blocked = task.subtasks.filter((s) =>
				s.status === "pending" && s.dependsOn.length > 0 &&
				s.dependsOn.some((depId) => { const dep = subById.get(depId); return !dep || dep.status !== "completed"; })
			).length;
			const blockTag = blocked > 0 ? ` ·${blocked}⏳` : "";
			const countTag = total > 0 ? `${completed}/${total}${failTag}${runTag}${blockTag} ` : "";
			// ponytail: budget desc by the ACTUAL prefix length — cursor(2)+badge(4)+
			// space+id(14)+space+countTag(varies)+space+age(1+age.len). The old fixed
			// width-34 assumed a 4-char countTag ("2/3 "), but "10/10 " is 6 — the
			// row overflowed width and the compositor truncated the desc right side.
			const prefixLen = 2 + 4 + 1 + 14 + 1 + countTag.length + 1 + age.length + 1;
			// ponytail: ellipsis on a truncated desc — the slice was bare, so a long
			// description cut silently with no signal there was more. Mirrors the
			// subtask-tree row (#449). Reserve 1 col for "…" and append only when the
			// desc overflows the budget; a desc that fits stays verbatim.
			const budget = Math.max(0, width - prefixLen);
			const fullDesc = task.description;
			const truncated = fullDesc.length > budget;
			const desc = truncated
				? fullDesc.slice(0, Math.max(0, budget - 1)) + (budget > 0 ? "…" : "")
				: fullDesc.slice(0, budget);

			lines.push(`  ${cursor} ${badge} ${task.id.slice(0, 14)} ${countTag}${desc}${ageSuffix}`);
		}

		if (tasks.length > this.maxVisible) {
			lines.push(this.scrollFooter(this.scrollOffset, this.maxVisible, tasks.length));
		}

		// ponytail: flashMsg after footer so it doesn't shift list rows.
		if (this.flashMsg) {
			lines.push(this.theme.fg("dim", `  ${this.flashMsg.slice(0, Math.max(0, width - 2))}`));
		}

		return lines;
	}

	private renderDetail(width: number): string[] {
		// ponytail: refresh detailLines from the live task on each render — the
		// snapshot taken in openDetail goes stale when the task's status/subtasks
		// change (external event, or the detail's own c/p/r firing), so the detail
		// view would show the old status until reopened. F7's 1s tick calls render,
		// so this picks up live state. Falls back to the cached snapshot when the
		// task is no longer resolvable (evicted). Re-clamp scroll in case the line
		// count shrank below the current offset.
		if (this.detailTaskId) {
			const live = this.opts.getTask ? this.opts.getTask(this.detailTaskId) : undefined;
			if (live) this.detailLines = formatTaskDetail(live, this.theme, width);
			if (this.detailScroll > 0 && this.detailScroll >= this.detailLines.length) {
				this.detailScroll = Math.max(0, this.detailLines.length - this.maxVisible);
			}
		}
		const lines: string[] = [];
		lines.push(this.theme.fg("accent", `  Task ${this.detailTaskId?.slice(0, 14) ?? ""}`));
		// ponytail: detail hint uses hintLine too — the full hint (~78) fits an 80-col
		// terminal but truncates on narrower ones, dropping Esc/q back (the close path).
		// Compact keeps the scroll + action keys + Esc visible. Mirrors list/tree (#507).
		lines.push(this.theme.fg("dim", this.hintLine(width,
			"  ↑↓/jk scroll · c cancel · p pause · r resume · t subtask tree · n next-failed · N prev-failed · y/Y copy · Esc/q back",
			"  ↑↓/jk scroll · c/p/r · n/N failed · y/Y copy · Esc back",
		)));
		lines.push("");
		const maxVisible = this.maxVisible;
		const start = this.detailScroll;
		const slice = this.detailLines.slice(start, start + maxVisible);
		for (const l of slice) {
			// ponytail: don't raw-slice - detailLines carry ANSI theme codes, and
			// String.slice would split an escape sequence (color bleed / garble)
			// and over-truncate (ANSI bytes eat the visible-width budget). The
			// overlay compositor already truncates each line to terminal width
			// ANSI-safely (sliceByColumn), so emit the full line and let it clip.
			lines.push(`  ${l}`);
		}
		if (this.detailLines.length > maxVisible) {
			lines.push(this.scrollFooter(start, maxVisible, this.detailLines.length));
		}
		// ponytail: F1 — detail mode must show flashMsg too. Before this, setFlash()
		// in detail (c/p/r success/fail, `/` hint) set state the renderer never
		// emitted, silently nullifying the S3/S4 feedback fixes in detail mode.
		if (this.flashMsg) {
			lines.push(this.theme.fg("dim", `  ${this.flashMsg.slice(0, Math.max(0, width - 2))}`));
		}
		return lines;
	}

	// ponytail: F6 — scroll footer with direction arrows. The bare "X-Y of Z"
	// count doesn't tell the user content ABOVE is clipped once scrollOffset > 0;
	// ▲/▼ do. Arrows only appear on the clipped side; no extra lines.
	private scrollFooter(offset: number, visible: number, total: number): string {
		const up = offset > 0 ? "▲ " : "";
		const down = offset + visible < total ? " ▼" : "";
		return this.theme.fg("dim", `  ${up}${offset + 1}-${Math.min(offset + visible, total)} of ${total}${down}`);
	}

	private formatAge(ts: number): string {
		// ponytail: clamp negative diff to 0 — a future createdAt (clock skew
		// between client and server, or a ts set slightly ahead) produced
		// "-5s ago" without this. "0s ago" is the honest floor.
		const diff = Math.max(0, Date.now() - ts);
		if (diff < 60_000) return `${Math.floor(diff / 1000)}s ago`;
		if (diff < 3600_000) return `${Math.floor(diff / 60_000)}m ago`;
		if (diff < 86400_000) return `${Math.floor(diff / 3600_000)}h ago`;
		return `${Math.floor(diff / 86400_000)}d ago`;
	}

	// ponytail: the list row's age suffix — a terminal task (completed/failed/
	// cancelled) with completedAt shows "done {age}" (how long ago it finished),
	// mirroring the detail header's doneTag (#430). The plain "{age}" (now-
	// createdAt) read as "still going" for a task that ended 5m ago — misleading
	// in the list. Falls back to createdAt-age when completedAt is absent or the
	// task is in-flight (no completion to report).
	private taskAge(task: TaskState): string {
		if (task.completedAt && ["completed", "failed", "cancelled"].includes(task.status)) {
			return `done ${this.formatAge(task.completedAt)}`;
		}
		return this.formatAge(task.createdAt);
	}

	// ponytail: restore cursor to the pre-filter cursor task (if still present in
	// the now-unfiltered list), else row 0. Called when the filter is cleared so
	// the user doesn't lose their place. Clears the capture so a subsequent filter
	// starts fresh.
	private restoreCursorAfterFilter(): void {
		const tasks = this.currentTasks();
		if (this.preFilterCursorTaskId) {
			const idx = tasks.findIndex((t) => t.id === this.preFilterCursorTaskId);
			if (idx >= 0) {
				this.cursorIdx = idx;
				this.scrollOffset = 0;
				this.clampCursorAndScroll();
				this.preFilterCursorTaskId = null;
				return;
			}
		}
		this.cursorIdx = 0;
		this.scrollOffset = 0;
		this.preFilterCursorTaskId = null;
	}

	// ponytail: clamp cursor into filtered bounds — query changes can shrink
	// the list below the current cursor, so snap it back. Also clamps scroll.
	private clampCursorAndScroll(): void {
		const tasks = this.currentTasks();
		if (this.cursorIdx >= tasks.length) {
			this.cursorIdx = Math.max(0, tasks.length - 1);
		}
		if (this.cursorIdx < this.scrollOffset) this.scrollOffset = this.cursorIdx;
		else if (this.cursorIdx >= this.scrollOffset + this.maxVisible) {
			this.scrollOffset = this.cursorIdx - this.maxVisible + 1;
		}
		if (this.scrollOffset < 0) this.scrollOffset = 0;
	}

	handleInput(data: string): void {
		if (this.detailTaskId) {
			// detail scroll mode — filter does not apply in detail mode
			// ponytail: `q` mirrors list mode (consistency) — Esc is the documented
			// back key, but muscle memory from list-mode `q` should also work here.
			if (data === KEY.esc || data === "q") {
				this.detailTaskId = null;
				this.detailScroll = 0;
				return;
			}
			// ponytail: S4 — `/` in detail mode gives feedback instead of silent no-op.
			// Detail is a single-task view, no list to filter; tell the user why.
			if (data === "/") {
				this.setFlash("filter not available in detail view");
				return;
			}
			// ponytail: F22 — `y` yanks the detail task's id; `Y` yanks its error
			// text (mirrors list-mode `Y` + the subtask-tree's `Y`).
			if (data === "y") {
				if (this.detailTaskId) this.yank(this.detailTaskId);
				return;
			}
			if (data === "Y") {
				const dt = this.opts.getTask ? this.opts.getTask(this.detailTaskId!) : undefined;
				// ponytail: fall back to the first failed subtask's error when the task
				// has none (mirrors list-mode `Y`). getTask unavailable → generic flash.
				const err = dt ? this.taskErrorToYank(dt) : undefined;
				if (err) this.yank(err);
				else this.setFlash("no error to copy");
				return;
			}
			// ponytail: S3 — detail-mode quick actions fire on the detail's own task.
			// Single-tap (no double-tap confirm): detail is a focused single-task view,
			// the user already chose this task. fireAction clears pendingCancel itself.
			if (data === "c" || data === "C") {
				// ponytail: mirror list-mode cancel guard — terminal statuses (completed/
				// cancelled) can't be cancelled; flash instead of firing an action the
				// server rejects. Falls through to fireAction when getTask is unavailable.
				const dt = this.opts.getTask ? this.opts.getTask(this.detailTaskId!) : undefined;
				if (dt && (dt.status === "completed" || dt.status === "cancelled")) {
					this.setFlash(`already ${dt.status}`);
					return;
				}
				this.fireAction(this.detailTaskId, "cancel", "cancelled");
				return;
			}
			if (data === "p") {
				// ponytail: mirror list-mode pause terminal-status guard — completed/
				// cancelled can't be paused; flash. Falls through to fireAction when
				// getTask is unavailable.
				const dt = this.opts.getTask ? this.opts.getTask(this.detailTaskId!) : undefined;
				if (dt && (dt.status === "completed" || dt.status === "cancelled")) {
					this.setFlash(`already ${dt.status}`);
					return;
				}
				this.fireAction(this.detailTaskId, "pause", "paused");
				return;
			}
			if (data === "r") {
				// ponytail: mirror list-mode resume terminal-status guard — completed/
				// cancelled can't be resumed; flash. Falls through to fireAction when
				// getTask is unavailable.
				const dt = this.opts.getTask ? this.opts.getTask(this.detailTaskId!) : undefined;
				if (dt && (dt.status === "completed" || dt.status === "cancelled")) {
					this.setFlash(`already ${dt.status}`);
					return;
				}
				this.fireAction(this.detailTaskId, "resume", "resumed");
				return;
			}
			// ponytail: `t` — jump from this task's detail to its subtask tree (the
			// reverse of the tree's `d` → task detail). Close the list first so
			// overlays don't stack; the tree opener re-opens its own overlay.
			if (data === "t") {
				if (!this.detailTaskId) {
					this.setFlash("no task selected");
				} else if (!this.opts.onJumpToTree) {
					this.setFlash("jump unavailable");
				} else {
					// ponytail: guard a 0-subtask task — jumping to the subtask tree
					// would open an empty tree ("No tasks"), wasting a round-trip.
					// The detail view already shows the (empty) subtask list, so flash
					// instead. Falls through to the jump when getTask is unavailable.
					const dt = this.opts.getTask ? this.opts.getTask(this.detailTaskId) : undefined;
					if (dt && dt.subtasks.length === 0) {
						this.setFlash("no subtasks");
					} else {
						const id = this.detailTaskId;
						this.done();
						this.opts.onJumpToTree(id);
					}
				}
				return;
			}
			// ponytail: `n` in detail — jump to the NEXT failed task (mirrors list-mode
			// `n`), exit detail back to list with cursor on the failed task. Wraps
			// around the (filtered) set. No failed → flashMsg, stay in detail.
			if (data === "n") {
				const tasks = this.currentTasks();
				const start = this.cursorIdx + 1;
				let next = -1;
				for (let i = 0; i < tasks.length; i++) {
					const idx = (start + i) % tasks.length;
					if (this.isTaskFailed(tasks[idx])) { next = idx; break; }
				}
				if (next < 0) {
					this.setFlash("no failed tasks");
				} else if (next === this.cursorIdx) {
					// ponytail: sole failed task — `n` wrapped back to the cursor itself.
					// Flash instead of silently exiting detail (which the else branch
					// does by clearing detailTaskId). Mirrors the list-mode sole-failed fix.
					this.setFlash("only failed task");
				} else {
					this.cursorIdx = next;
					this.detailTaskId = null;
					this.detailScroll = 0;
					this.flashMsg = null;
					this.clampCursorAndScroll();
				}
				return;
			}
			if (data === "N") {
				// ponytail: `N` in detail — prev-failed (the complement of detail `n`).
				// list-mode has N (#420); detail-mode didn't, so `N` was a dead key here.
				// Jump to the PREV failed task, exit detail back to list with cursor on it.
				// Wraps around the (filtered) set. No failed → flashMsg, stay in detail.
				const tasks = this.currentTasks();
				const start = this.cursorIdx - 1;
				let prev = -1;
				for (let i = 0; i < tasks.length; i++) {
					const idx = ((start - i) % tasks.length + tasks.length) % tasks.length;
					if (this.isTaskFailed(tasks[idx])) { prev = idx; break; }
				}
				if (prev < 0) {
					this.setFlash("no failed tasks");
				} else if (prev === this.cursorIdx) {
					// ponytail: sole failed task — `N` wrapped back to the cursor itself.
					// Flash instead of silently exiting detail. Mirrors list-mode N sole-failed.
					this.setFlash("only failed task");
				} else {
					this.cursorIdx = prev;
					this.detailTaskId = null;
					this.detailScroll = 0;
					this.flashMsg = null;
					this.clampCursorAndScroll();
				}
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

		// ── filter-editing mode: intercept printable/backspace/esc/enter ──────
		if (this.searchMode) {
			if (data === KEY.esc) {
				// Esc in search mode: clear query + exit (full list restored)
				this.query = "";
				this.searchMode = false;
				this.restoreCursorAfterFilter();
				return;
			}
			if (data === KEY.enter || data === "\n") {
				// Enter: exit editing but KEEP the filter active for navigation
				this.searchMode = false;
				this.clampCursorAndScroll();
				return;
			}
			if (data === "\x7f" || data === "\b") {
				// Backspace: drop last char; stay in filter mode even if empty
				this.query = this.query.slice(0, -1);
				this.clampCursorAndScroll();
				return;
			}
			// ponytail: command keys that fall through to normal handling even in
			// search-edit (exit editing, keep filter, then act). Listed here so the
			// printable-char branch below does NOT swallow them into the query — c/C
			// (cancel), p (pause), r (resume), y (copy id), n (next-failed) would
			// otherwise append to the query and be unreachable while editing. `t` is
			// NOT here: it's a letter users type into filters ("beta"), so in
			// search-edit it appends to the query; exit editing (Esc/Enter/nav) first
			// to use `t` as a jump. `Y` joins the fall-through set (yank error).
			// Mirrors the subtask-tree fix.
			const searchCmdKeys = ["c", "C", "p", "r", "y", "Y", "n", "N"];
			// ponytail: printable single char (ASCII 0x20..0x7e, includes `/` itself)
			// EXCEPT the command keys above (those fall through to normal handling).
			if (data.length === 1 && data >= " " && data <= "~" && !searchCmdKeys.includes(data)) {
				this.query += data;
				this.clampCursorAndScroll();
				return;
			}
			// ponytail: nav keys (arrows/page/home/end/g/G/j/k) in search-edit:
			// exit editing (keep filter) then FALL THROUGH to normal handler so
			// the cursor moves within the filtered set in one keystroke.
			const navKeys = [KEY.up, KEY.down, KEY.pageUp, KEY.pageDown, KEY.home, KEY.end, "g", "G", "j", "k"];
			if (navKeys.includes(data)) {
				this.searchMode = false;
				// fall through to normal handling below
			} else if (searchCmdKeys.includes(data)) {
				// command key in search-edit: exit editing (keep filter) then fall through
				this.searchMode = false;
				// fall through to normal handling below
			} else {
				// Unknown control sequence in search mode — ignore (don't exit)
				return;
			}
		}

		// ── normal (non-search) mode ─────────────────────────────────────────
		// ponytail: clear flashMsg on any key that isn't c/C so nav/enter/esc
		// dismisses the confirm/abort hint. c/C refreshes it instead.
		if (this.flashMsg && data !== "c" && data !== "C") {
			this.flashMsg = null;
		}
		// ponytail: also clear pendingCancel on any non-c/C key — the armed
		// double-tap is for the CURRENT cursor task; if the user navs away (even
		// back to the original task) or hits another action, the intent changed.
		// Without this, arm t1 → nav t2 → nav t1 → `c` would re-match the stale
		// pendingCancel===t1 and fire cancel t1 the user no longer expects.
		if (this.pendingCancel && data !== "c" && data !== "C") {
			this.pendingCancel = null;
		}
		if (data === "/") {
			// Enter filter mode (or resume editing an existing filter)
			// ponytail: capture the pre-filter cursor task so clearing the filter
			// restores focus to it (if still present) instead of row 0. Only capture
			// when entering fresh (query empty) — resuming an active filter keeps the
			// original capture.
			if (!this.query) {
				const t = this.currentTasks()[this.cursorIdx];
				this.preFilterCursorTaskId = t ? t.id : null;
			}
			this.searchMode = true;
			return;
		}

		if (data === KEY.esc || data === "q") {
			// ponytail: if a filter is active, Esc clears it first (stay open);
			// only a second Esc (or Esc with no filter) closes the overlay.
			if (this.query) {
				this.query = "";
				this.restoreCursorAfterFilter();
				return;
			}
			this.done();
			return;
		}

		const tasks = this.currentTasks();

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
			// ponytail: S6 — Enter on an empty / filtered-empty list sets a flashMsg so
			// the user knows the keystroke was received, instead of silent no-op.
			// Mirrors the subtask-tree's Enter empty-list feedback.
			const task = tasks[this.cursorIdx];
			if (task) this.openDetail(task.id);
			else this.flashMsg = "no task selected";
		} else if (data === "c" || data === "C") {
			// ponytail: double-tap cancel. First c arms (flashMsg naming the task);
			// second c fires onAction. Any non-c key clears (handled at top of fn).
			const task = tasks[this.cursorIdx];
			// ponytail: dead-key feedback for terminal statuses — a completed/cancelled
			// task can't be cancelled, so don't arm the double-tap (which would mislead
			// the user into a second `c` that fires an action the server will reject).
			// `failed` is left to the server (cancel-after-fail semantics differ).
			if (task && (task.status === "completed" || task.status === "cancelled")) {
				this.pendingCancel = null;
				this.flashMsg = `already ${task.status}`;
			} else if (task && this.opts.onAction) {
				if (this.pendingCancel === task.id) {
					// ponytail: await result via .then (handleInput is sync, can't be async)
					// so confirmation feedback lands after the orchestrator resolves.
					this.flashMsg = "cancelling…";
					this.fireAction(task.id, "cancel", "cancelled");
					this.pendingCancel = null;
				} else {
					this.pendingCancel = task.id;
					// ponytail: show the FULL task id — cancel is destructive, a truncated
					// prefix risks the user confirming the wrong task (multiple tasks can
					// share an 8-char prefix). The flashMsg line is width-clamped at render
					// (slice width-2), so a long id just right-truncates there. Mirrors the
					// yank flash full-id fix.
					this.flashMsg = `press c again to cancel ${task.id} (any other key aborts)`;
				}
			} else if (task) {
				this.flashMsg = "cancel unavailable";
			}
		} else if (data === "p") {
			// ponytail: mirror the cancel/resume terminal-status guards — a completed/
			// cancelled task can't be paused, so flash instead of firing onAction(pause)
			// the server would reject. `in_progress`/`planning` are pausable; left to server.
			const task = tasks[this.cursorIdx];
			// ponytail: F3 — mirror c's "cancel unavailable" so p isn't a silent dead key.
			if (task && (task.status === "completed" || task.status === "cancelled")) {
				this.flashMsg = `already ${task.status}`;
			} else if (task && this.opts.onAction) this.fireAction(task.id, "pause", "paused");
			else if (task) this.flashMsg = "pause unavailable";
		} else if (data === "r") {
			// ponytail: mirror the cancel terminal-status guard — a completed/cancelled
			// task can't be resumed (terminal), so flash instead of firing an action
			// the server rejects. `failed`/`paused` are the resumable statuses; left
			// to the server when the cursor isn't terminal.
			const task = tasks[this.cursorIdx];
			if (task && (task.status === "completed" || task.status === "cancelled")) {
				this.flashMsg = `already ${task.status}`;
			} else if (task && this.opts.onAction) this.fireAction(task.id, "resume", "resumed");
			else if (task) this.flashMsg = "resume unavailable";
		} else if (data === "y" || data === "Y") {
			// ponytail: F22 — `y` yanks cursor task id; `Y` yanks its error text
			// (mirrors the subtask-tree's `Y`). No error → flash, so the user
			// knows `Y` registered but nothing to copy.
			const task = tasks[this.cursorIdx];
			if (!task) {
				this.flashMsg = "no task selected";
			} else if (data === "Y") {
				// ponytail: yank the task error, falling back to the first failed
				// subtask's error when the task itself has none (an in_progress task
				// with a failed subtask). "no error to copy" only when neither exists.
				const err = this.taskErrorToYank(task);
				if (err) this.yank(err);
				else this.flashMsg = "no error to copy";
			} else {
				this.yank(task.id);
			}
		} else if (data === "n") {
			// ponytail: jump to the NEXT failed TASK after the cursor — the subtask-tree
			// has n/p for subtask-level; the task-list had no task-level jump, so finding
			// the next failed task among many meant ↓ scrolling or /failed filter. `n`
			// jumps cursor to the next task with status "failed", wrapping to the first
			// when past the last (repeated `n` cycles). `N` is prev-failed (below) since
			// `p` is pause — no lowercase prev. No failed → flashMsg.
			const start = this.cursorIdx + 1;
			let next = -1;
			for (let i = 0; i < tasks.length; i++) {
				const idx = (start + i) % tasks.length;
				if (this.isTaskFailed(tasks[idx])) { next = idx; break; }
			}
			if (next < 0) {
				this.flashMsg = "no failed tasks";
			} else if (next === this.cursorIdx) {
				// ponytail: sole failed task — `n` wrapped back to the cursor itself,
				// so there's nowhere to jump. Flash so the user knows `n` registered
				// (otherwise silent no-op: cursor unmoved, flash null).
				this.flashMsg = "only failed task";
			} else {
				this.cursorIdx = next;
				this.flashMsg = null;
			}
		} else if (data === "N") {
			// ponytail: jump to the PREV failed task before the cursor — the complement
			// of `n`. The subtask-tree uses lowercase p/n for prev/next-failed, but the
			// task-list's `p` is pause, so there was no prev path: a user who overshot
			// a failure with `n` had to `/failed` filter or ↑ scroll. `N` (uppercase,
			// unused) mirrors vim's N/n next-prev convention and costs nothing. Wraps to
			// the last failed when the cursor is at/before the first. No failed → flashMsg.
			const start = this.cursorIdx - 1;
			let prev = -1;
			for (let i = 0; i < tasks.length; i++) {
				const idx = ((start - i) % tasks.length + tasks.length) % tasks.length;
				if (this.isTaskFailed(tasks[idx])) { prev = idx; break; }
			}
			if (prev < 0) {
				this.flashMsg = "no failed tasks";
			} else if (prev === this.cursorIdx) {
				// ponytail: sole failed task — `N` wrapped back to the cursor itself.
				// Flash so the user knows `N` registered (otherwise silent no-op).
				this.flashMsg = "only failed task";
			} else {
				this.cursorIdx = prev;
				this.flashMsg = null;
			}
		} else if (data === "t") {
			// ponytail: `t` in list — jump from the cursor task straight to its
			// subtask tree, mirroring detail-mode `t` (the reverse of the tree's
			// `d` → detail). Without this, list-mode `t` was a silent no-op; the
			// user had to Enter→detail→t (two keys). No cursor task / no opener
			// → flashMsg instead of silent no-op.
			const task = tasks[this.cursorIdx];
			if (!task) {
				this.setFlash("no task selected");
			} else if (!this.opts.onJumpToTree) {
				this.setFlash("jump unavailable");
			} else if (task.subtasks.length === 0) {
				// ponytail: guard a 0-subtask task — jumping would open an empty
				// tree ("No tasks"), wasting a round-trip. The list row already
				// shows the count (or omits it via the 0-subtask guard), so flash.
				this.setFlash("no subtasks");
			} else {
				const id = task.id;
				this.done();
				this.opts.onJumpToTree(id);
			}
		}
		// clamp scroll to cursor
		this.clampCursorAndScroll();
	}

	// ponytail: fire onAction + set in-overlay flashMsg on success. handleInput is
	// sync (pi-tui interface), so resolve the (possibly async) onAction via .then.
	// Failure is surfaced by extension.ts notify(); overlay only confirms success
	// so the user sees the action landed without leaving the overlay.
	private fireAction(taskId: string, action: "cancel" | "pause" | "resume", verb: string): void {
		// ponytail: F3 — list-mode callers guard onAction before calling (c shows
		// "cancel unavailable", p/r show their own), so this branch only fires for
		// detail-mode c/p/r, which previously no-op'd silently without onAction.
		if (!this.opts.onAction) {
			this.setFlash(`${action} unavailable`);
			return;
		}
		// ponytail: pending-armed state only applies to cancel; clear it for pause/resume
		// so a leftover pendingCancel doesn't linger across a p/r press.
		this.pendingCancel = null;
		try {
			const ret = this.opts.onAction(taskId, action);
			if (ret && typeof (ret as Promise<boolean>).then === "function") {
				(ret as Promise<boolean>).then((ok) => {
					// ponytail: F2 — ok=false must replace the "cancelling…" flash,
					// else it lingers until the next keypress despite the action failing.
					// Full id (not slice(0,8)): mirrors the cancel-armed flash — multiple
					// tasks can share an 8-char prefix, so a truncated confirm flash is
					// ambiguous. The flashMsg line is width-clamped at render.
					if (ok) this.setFlash(`${verb} ${taskId}`);
					else this.setFlash(`${action} failed`);
				});
			} else if (ret) {
				this.setFlash(`${verb} ${taskId}`);
			} else {
				this.setFlash(`${action} failed`);
			}
		} catch {
			// swallow — extension.ts surfaces failures via notify()
		}
	}

	private setFlash(msg: string): void {
		this.flashMsg = msg;
		(this.tui as any)?.requestRender?.();
	}

	// ponytail: F22 — yank with in-overlay feedback. opts.copy is injectable so
	// selfchecks don't touch the real clipboard; default is the system clipboard.
	private yank(text: string): void {
		const ok = (this.opts.copy ?? copyText)(text);
		// ponytail: show the FULL id copied, not a truncated prefix — the old
		// slice(0,8) rendered `copied task_abc` for a `task_abc123def` id, which
		// looked like a partial copy and hid what actually landed on the clipboard.
		// The flashMsg line is itself width-clamped at render (slice width-2), so a
		// long id just right-truncates there instead of misleading the user here.
		this.setFlash(ok ? `copied ${text}` : "copy failed");
	}

	// ponytail: the error text `Y` should yank for a task. Prefer the task-level
	// error, but fall back to the FIRST failed subtask's error — a task stays
	// in_progress while a subtask fails (the common triage state), so task.error
	// is empty and the failure the user wants to paste lives on the subtask.
	// Mirrors the progress-widget's "first failed subtask error" surfacing.
	private taskErrorToYank(task: TaskState): string | undefined {
		if (task.error) return task.error;
		const failed = task.subtasks.find((s) => s.status === "failed" && s.error);
		return failed?.error;
	}

	// ponytail: a task counts as "failed" for triage when its status is failed OR a
	// subtask failed — an in_progress task with a failed subtask is the common triage
	// target (the ·N✗ row marker, /failed filter, and header count all use this).
	// Shared so n/N next/prev-failed jumps land on the SAME set the markers advertise
	// (was status==="failed" only, skipping in_progress tasks with failed subtasks).
	private isTaskFailed(task: TaskState): boolean {
		return task.status === "failed" || task.subtasks.some((s) => s.status === "failed");
	}

	private openDetail(taskId: string): void {
		const task = this.opts.getTask ? this.opts.getTask(taskId) : this.opts.tasks().find((t) => t.id === taskId);
		if (!task) return;
		this.detailTaskId = taskId;
		this.detailLines = formatTaskDetail(task, this.theme);
		this.detailScroll = 0;
		// ponytail: align list cursor to the detail task so Esc→list lands the focus
		// on it (jump-from-subtask-tree `d` opens detail via initialDetailTaskId with
		// cursorIdx still 0; without this, Esc back to list left the cursor on the
		// first task, so a subsequent c/p/r would hit the wrong task). Respect any
		// active filter — if the task isn't in the filtered set, leave cursor as-is.
		const idx = this.currentTasks().findIndex((t) => t.id === taskId);
		if (idx >= 0) {
			this.cursorIdx = idx;
			this.clampCursorAndScroll();
		}
	}

	invalidate(): void {}
	dispose(): void {
		if (this.refreshTimer) {
			clearInterval(this.refreshTimer);
			this.refreshTimer = null;
		}
	}
}
