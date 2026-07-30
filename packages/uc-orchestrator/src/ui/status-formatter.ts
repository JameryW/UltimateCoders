/**
 * StatusFormatter — structured /uc status output.
 *
 * Replaces plain-text notify() output with styled rendering
 * using Theme colors for status icons, progress, and DAG visualization.
 */

import type { Theme } from "@oh-my-pi/pi-coding-agent";
import type { TaskState, SubtaskResult } from "../orchestrator/orchestrator";
import { formatErrorForDisplay } from "./error-format";
import { statusIcon } from "./status-icons";
import { formatElapsed } from "./elapsed";

// ── Task List (no task ID) ───────────────────────────────────────

// ponytail: width budget helpers. /uc status renders via notify() (toast),
// NOT the overlay compositor — so there is no ANSI-aware truncation backstop.
// Long description/error lines wrap and garble the toast. Slice the PLAIN
// content BEFORE wrapping it in theme.fg (NEVER raw-slice a themed string —
// that splits escape sequences, see [[tui-overlay-rendering-constraints]]).
// `width` is optional: the overlay detail path passes the live render width;
// notify callers pass ctx.ui.terminal.columns. Undefined → legacy fixed caps.
function cap(text: string, budget: number | undefined, fallback: number): string {
	const b = typeof budget === "number" && budget > 0 ? budget : fallback;
	return text.length > b ? text.slice(0, Math.max(0, b - 1)) + "…" : text;
}

export function formatTaskList(tasks: TaskState[], theme: Theme, width?: number): string[] {
	if (tasks.length === 0) {
		// ponytail: mirror the overlay empty-state (#422) — a /uc status to an empty
		// store showed only "No tasks", with no cue how to add one.
		return [
			theme.fg("dim", "No tasks"),
			theme.fg("dim", "/uc submit <description> to add one"),
		];
	}

	const lines: string[] = [];
	// ponytail: count header — a /uc status toast listed tasks with no heading,
	// so the user didn't know how many to expect at a glance. One dim line (mirrors
	// the overlay's "N task(s)" headerExtra). Skipped when 0 (the empty-state path
	// above already returns).
	lines.push(theme.fg("dim", `${tasks.length} task(s):`));
	for (const i of tasks.keys()) {
		const task = tasks[i];
		// ponytail: blank line between tasks (not before the first) — a /uc status
		// toast with many tasks was a wall of text; the blank separates per-task
		// (id+status / description) pairs so the user can visually chunk.
		if (i > 0) lines.push("");
		const icon = statusIcon(task.status, theme);
		const completed = task.subtasks.filter((s) => s.status === "completed").length;
		const failed = task.subtasks.filter((s) => s.status === "failed").length;
		const total = task.subtasks.length;
		// ponytail: only show completed/total when the task HAS subtasks — a
		// 0-subtask task rendered "0/0", which read as "0 completed" instead of
		// "no subtasks". Mirrors the task-list overlay row + formatTaskDetail guards.
		// failed-count marker ("·N✗") mirrors the overlay row (#429): 3/5 with 2 failed
		// read "3/5" like 3 completed + 2 pending, hiding the failure in /uc status too.
		const failTag = failed > 0 ? `·${failed}✗ ` : "";
		// ponytail: running-count marker — mirrors the detail countTag (#461) + widget
		// bar (#460). The notify row showed failed count but not how many subtasks are
		// actively running, so "is this making progress" needed a separate /uc status <id>.
		const running = task.subtasks.filter((s) => s.status === "running" || s.status === "reviewing").length;
		const runTag = running > 0 ? `·${running}▶ ` : "";
		// ponytail: blocked-count marker — a pending subtask with unmet deps is
		// "blocked" (mirrors the detail view's ⏳N, #473). Showing the aggregate count
		// at the task level answers "is this task stalled on deps" without opening
		// the detail. Only when blocked > 0 (no noise on healthy tasks).
		const subById = new Map(task.subtasks.map((s) => [s.id, s]));
		const blocked = task.subtasks.filter((s) =>
			s.status === "pending" && s.dependsOn.length > 0 &&
			s.dependsOn.some((depId) => { const dep = subById.get(depId); return !dep || dep.status !== "completed"; })
		).length;
		const blockTag = blocked > 0 ? `·${blocked}⏳ ` : "";
		const countTag = total > 0 ? `${completed}/${total} ${failTag}${runTag}${blockTag}` : "";
		const ctrl = task.controlState !== "running" ? ` [${task.controlState}]` : "";
		// ponytail: age tag for in-flight tasks — mirrors the overlay row's taskAge
		// (#443) + detail header's ageTag (#421). The notify row showed done-time
		// for terminal tasks but no time signal for in-flight ones, so "how long has
		// this been running" needed opening the detail. Show "(Ns)" (now-createdAt)
		// for in_progress/planning/paused. Done-time stays for terminal tasks.
		const ageTag = ["in_progress", "planning", "paused"].includes(task.status)
			? ` (${formatElapsed(Date.now() - task.createdAt)})` : "";
		const doneTag = task.completedAt && ["completed", "failed", "cancelled"].includes(task.status)
			? ` (done ${formatElapsed(Date.now() - task.completedAt)} ago`
				// ponytail: total run duration — mirrors the widget ⏱ (#454) + detail
				// header (#455). Append " ·⏱Ns" (createdAt→completedAt) so the notify
				// row answers "did this take unusually long" without opening the detail.
				+ ` ·⏱${formatElapsed(task.completedAt - task.createdAt)})`
			: "";
		lines.push(`${icon} ${task.id.slice(0, 14)} ${countTag}${task.status}${ctrl}${theme.fg("dim", ageTag || doneTag)}`);
		// ponytail: `  ` indent + "Description" label eat ~14 cols of the desc
		// budget; cap the plain desc so the toast line fits the terminal.
		lines.push(theme.fg("dim", `  ${cap(task.description, width !== undefined ? width - 2 : undefined, 60)}`));
	}
	return lines;
}

// ── Task Detail (with task ID) ───────────────────────────────────

export function formatTaskDetail(task: TaskState, theme: Theme, width?: number): string[] {
	const lines: string[] = [];
	const icon = statusIcon(task.status, theme);
	// ponytail: mirror formatTaskList — pause leaves status as in_progress but
	// sets controlState="paused"; without this the detail header reads
	// "in_progress" for a paused task. cancelled already flips status.
	const ctrl = task.controlState !== "running" ? ` [${task.controlState}]` : "";
	// ponytail: mirror formatTaskList's completed/total — the detail header listed
	// subtasks below but the header itself had no count summary, so the user had to
	// count rows to gauge progress. Only when there are subtasks (a 0-subtask task
	// would show a misleading "0/0").
	const total = task.subtasks.length;
	const completed = task.subtasks.filter((s) => s.status === "completed").length;
	const failed = task.subtasks.filter((s) => s.status === "failed").length;
	// ponytail: failed-count marker in the detail countTag — mirrors the overlay row
	// (#429) + notify list (#436). The detail header showed only completed/total, so a
	// 3/5 task with 2 failed read "3/5" like 3 completed + 2 pending. Append " ·N✗"
	// only when failed > 0 (no noise on healthy tasks).
	const failTag = failed > 0 ? ` ·${failed}✗` : "";
	// ponytail: running-count marker in the detail countTag — mirrors the subtask-tree
	// header (#458) + widget bar (#460). The detail header's countTag showed
	// completed/total/failed, but not how many subtasks are actively running. Append
	// " ·N▶" only when running>0 (no noise on healthy/terminal tasks).
	const running = task.subtasks.filter((s) => s.status === "running" || s.status === "reviewing").length;
	const runTag = running > 0 ? ` ·${running}▶` : "";
	const countTag = total > 0 ? ` ${completed}/${total}${failTag}${runTag}` : "";
	// ponytail: live age on an in-flight task — mirrors the subtask-tree's running
	// elapsed tag. createdAt is the submit time, so "how long has this been going"
	// is now-createdAt. Only for in_progress/planning/paused (a running concern);
	// a completed/failed/cancelled task's age is stale and just noise. Dim + parens
	// so it reads as metadata, not a status field. (notify path is a frozen snapshot,
	// so the age is the render-time value — fine for a toast.)
	const ageTag = ["in_progress", "planning", "paused"].includes(task.status)
		? ` (${formatElapsed(Date.now() - task.createdAt)})` : "";
	// ponytail: terminal-task completion time — the complement of ageTag. A running
	// task shows live age; a terminal task (completed/failed/cancelled) shows when it
	// finished ("done 5m ago"), sourced from completedAt when present. Failed/
	// cancelled tasks also set completedAt (cancelTask/finish paths stamp it), so the
	// same tag answers "how long ago did this break/end". Skipped when completedAt is
	// absent (restored/older records) — no fallback to createdAt (that's "submitted",
	// not "finished", and would misread).
	const doneTag = task.completedAt && ["completed", "failed", "cancelled"].includes(task.status)
		? ` (done ${formatElapsed(Date.now() - task.completedAt)} ago`
			// ponytail: total run duration — mirrors the widget's ⏱ (#454). The time-signal
			// sweep covered when it ended, but not how long it ran; "⏱ Ns" (createdAt→
			// completedAt) answers "did this take unusually long" in /uc status <id> + detail.
			+ ` · ⏱${formatElapsed(task.completedAt - task.createdAt)})`
		: "";
	lines.push(`${icon} ${theme.bold(task.id)} — ${task.status}${ctrl}${countTag}${theme.fg("dim", ageTag || doneTag)}`);
	// ponytail: cap plain desc before theming — notify() toast has no ANSI-aware
	// truncation backstop (overlay detail does, but this fn feeds both paths).
	// F16: budget must subtract the "  Description: " prefix (15 cols) — the old
	// width-2 overflowed the line by ~13.
	lines.push(theme.fg("dim", `  Description: ${cap(task.description, width !== undefined ? width - 15 : undefined, 200)}`));
	if (task.error) {
		// ponytail: F16 — route through formatErrorForDisplay like the subtask
		// error path: classification label + ellipsis + ANSI-safe slicing. The old
		// raw slice ignored the prefix (overflowed ~7) and had no label/ellipsis.
		const errBudget = width !== undefined ? Math.max(0, width - 4) : 60;
		lines.push(`  ${formatErrorForDisplay(task.error, errBudget, (c, t) => theme.fg(c, t))}`);
	}

	lines.push("");
	// ponytail: subtask-status breakdown in the "Subtasks:" header — the rows below
	// show per-subtask status, but the header gave no at-a-glance tally. A failed
	// task's "Subtasks:" hid that 3 succeeded + 2 failed until the user scanned
	// every row. Append "(X done, Y failed, Z running)"-style counts (only non-zero
	// buckets, error-colored for failed) so the header summarizes the breakdown.
	const runningCount = task.subtasks.filter((s) => s.status === "running" || s.status === "reviewing").length;
	const cancelled = task.subtasks.filter((s) => s.status === "cancelled").length;
	const pending = task.subtasks.filter((s) => s.status === "pending").length;
	const other = total - completed - failed - runningCount - cancelled - pending;
	const parts: string[] = [];
	if (failed > 0) parts.push(theme.fg("error", `${failed} failed`));
	if (runningCount > 0) parts.push(`${runningCount} running`);
	if (pending > 0) parts.push(`${pending} pending`);
	if (cancelled > 0) parts.push(`${cancelled} cancelled`);
	if (other > 0) parts.push(`${other} other`);
	if (completed > 0 && completed < total) parts.push(`${completed} done`);
	const breakdown = parts.length > 0 ? ` (${parts.join(", ")})` : "";
	lines.push(theme.fg("accent", "Subtasks:") + theme.fg("dim", breakdown));

	// ponytail: true topological depth (longest dependency chain to a root),
	// not dependsOn.length. A subtask depending on 3 roots was indented to
	// depth 3 and grouped with real depth-3 nodes; now it's depth 1.
	const depthCache = new Map<string, number>();
	const subtaskById = new Map(task.subtasks.map((st) => [st.id, st]));
	const depthOf = (id: string, seen: Set<string>): number => {
		if (depthCache.has(id)) return depthCache.get(id)!;
		if (seen.has(id)) return 0; // ponytail: cycle guard — treat as root
		const st = subtaskById.get(id);
		if (!st || st.dependsOn.length === 0) {
			depthCache.set(id, 0);
			return 0;
		}
		seen.add(id);
		const d = 1 + Math.max(...st.dependsOn.map((d2) => depthOf(d2, seen)));
		seen.delete(id);
		depthCache.set(id, d);
		return d;
	};

	const byDeps = new Map<number, SubtaskResult[]>();
	for (const st of task.subtasks) {
		const depth = depthOf(st.id, new Set());
		if (!byDeps.has(depth)) byDeps.set(depth, []);
		byDeps.get(depth)!.push(st);
	}

	for (const [depth, subtasks] of [...byDeps.entries()].sort(([a], [b]) => a - b)) {
		// ponytail: within a depth tier, sort by status priority (failed first, then
		// running/reviewing, then everything else) so a failure surfaces to the top
		// of its tier instead of being buried under completed rows. Same-status
		// subtasks keep insertion order (stable sort). Mirrors the "failed-first"
		// triage intent without breaking the topological depth grouping.
		const statusRank = (s: string): number =>
			s === "failed" ? 0 : (s === "running" || s === "reviewing") ? 1 : 2;
		const tier = subtasks.slice().sort((a, b) => statusRank(a.status) - statusRank(b.status));
		for (const st of tier) {
			const stIcon = statusIcon(st.status, theme);
			const indent = "  ".repeat(depth + 1);
			const prefix = depth > 0 ? "↳ " : "";
			// ponytail: F25 — build deps PLAIN first so its length feeds the desc
			// budget (previously appended after capping, unbudgeted — many/long dep
			// ids overflowed the line). A dep list longer than half the width
			// collapses to "←+N deps". Theming happens after the plain decision,
			// so no themed string is ever raw-sliced.
			let depsPlain = "";
			if (st.dependsOn.length > 0) {
				const joined = st.dependsOn.join(",");
				depsPlain = width !== undefined && joined.length + 2 > width / 2
					? ` ←+${st.dependsOn.length} deps`
					: ` ←${joined}`;
			}
			// ponytail: "blocked" marker on a pending subtask whose deps aren't all
			// completed — a pending subtask with met deps is "ready to dispatch" while
			// one with unmet deps is "blocked". Without this, both looked identical (○
			// pending + deps), and the user couldn't tell if the subtask was waiting on
			// deps vs just not dispatched yet. Only on pending subtasks w/ deps; the
			// deps are checked against the task's subtask statuses.
			let blockedPlain = "";
			if (st.status === "pending" && st.dependsOn.length > 0) {
				const unmet = st.dependsOn.filter((depId) => {
					const dep = subtaskById.get(depId);
					return !dep || dep.status !== "completed";
				}).length;
				if (unmet > 0) blockedPlain = ` ⏳${unmet}`;
			}
			const deps = depsPlain ? theme.fg("dim", depsPlain) : "";
			// ponytail: "blocked" / "ready" markers on pending subtasks with deps.
			// A pending subtask with unmet deps is "blocked" (⏳N, #473), while one
			// with all deps met is "ready to dispatch" (✓) — both previously looked
			// identical to a pending subtask with no deps at all. Only on pending w/ deps.
			let readyPlain = "";
			if (st.status === "pending" && st.dependsOn.length > 0) {
				const unmet = st.dependsOn.filter((depId) => {
					const dep = subtaskById.get(depId);
					return !dep || dep.status !== "completed";
				}).length;
				readyPlain = unmet > 0 ? ` ⏳${unmet}` : " ✓";
			}
			const ready = readyPlain ? theme.fg("dim", readyPlain) : "";
			// ponytail: live elapsed on a running subtask — mirrors the subtask-tree
			// overlay's running-elapsed tag. /uc status <id> + the detail view showed a
			// running subtask with no time signal (only the tree overlay did). Built PLAIN
			// (before theming) so its length feeds the desc budget alongside depsPlain,
			// matching how the tree row subtracts the elapsed suffix.
			// A terminal subtask (completed/failed/cancelled) with completedAt shows
			// "(done Ns ago)" instead — the subtask-level mirror of the task-level doneTag
			// (#430). Running shows elapsed; terminal shows done-time; absent stamp → none.
			// ponytail: total run duration on terminal subtasks — mirrors the widget ⏱
			// (#454) + detail header (#455). Append " ·⏱Ns" (startedAt→completedAt) so a
			// subtask answers "did this take unusually long"; only when both stamps present.
			const durPlain = (st.status !== "running" && st.startedAt && st.completedAt
				&& ["completed", "failed", "cancelled"].includes(st.status))
				? ` ·⏱${formatElapsed(st.completedAt - st.startedAt)}` : "";
			const timePlain = st.status === "running" && st.startedAt
				? ` (${formatElapsed(Date.now() - st.startedAt)})`
				: (st.completedAt && ["completed", "failed", "cancelled"].includes(st.status))
					? ` (done ${formatElapsed(Date.now() - st.completedAt)} ago${durPlain})` : "";
			const elapsed = timePlain ? theme.fg("dim", timePlain) : "";
			// ponytail: retry×N on a failed subtask row — mirrors the subtask-tree's
			// collapsed failed-row retry tag (#439). A subtask that failed after N retries
			// is a "hard" failure; surfacing retryCount inline in /uc status <id> + the
			// detail view means the user sees it without expanding. Only when failed and
			// retryCount>0 (no "retry×0" noise; no tag on non-failed rows). Built PLAIN
			// so its length feeds the desc budget; appended last (dropped first on narrow).
			const retryPlain = st.status === "failed" && (st.retryCount ?? 0) > 0
				? ` retry×${st.retryCount}` : "";
			const retry = retryPlain ? theme.fg("dim", retryPlain) : "";
			// ponytail: cap desc to the remaining width after indent+icon+prefix+id+deps+elapsed+retry.
			// Without width (legacy notify path), keep the 50-char cap.
			// F17: icon VISIBLE width is 1 — stIcon.length includes ~11 ANSI escape
			// chars in real terminals, so the old subtraction over-truncated desc
			// by ~10 chars (the no-ANSI selfcheck theme hid it).
			const headPlain = `${indent}${prefix}${st.id}: `;
			// ponytail: declared-file count — SubtaskResult.files lists the files a
			// subtask intends to touch; a 12-file subtask is a wider blast radius than a
			// 1-file one. Only when files.length>0 (no noise on subtasks w/o declared
			// files). Built plain so its length feeds the desc budget; appended last.
			const filesPlain = (st.files?.length ?? 0) > 0
				? ` ${st.files!.length} file${st.files!.length === 1 ? "" : "s"}` : "";
			const files = filesPlain ? theme.fg("dim", filesPlain) : "";
			// ponytail: cap desc to the remaining width after indent+icon+prefix+id+deps+elapsed+retry+files.
			// Without width (legacy notify path), keep the 50-char cap.
			const descBudget = width !== undefined
				? Math.max(0, width - headPlain.length - 1 - 2 - depsPlain.length - timePlain.length - retryPlain.length - readyPlain.length - filesPlain.length)
				: 50;
			lines.push(`${indent}${stIcon} ${prefix}${st.id}: ${cap(st.description, descBudget, 50)}${deps}${elapsed}${retry}${ready}${files}`);

			if (st.error) {
				// ponytail: error budget = terminal width minus indent; default 60 for
				// the legacy notify path. formatErrorForDisplay slices plain error text
				// internally before theming, so passing a width is ANSI-safe.
				const errBudget = width !== undefined
					? Math.max(0, width - indent.length - 2)
					: 60;
				lines.push(`${indent}  ${formatErrorForDisplay(st.error, errBudget, (c, t) => theme.fg(c, t))}`);
			}
			// ponytail: success output — /uc status <id> + task-list detail showed a
			// subtask's error + retries + review but NOT its result (what it produced).
			// The task-result message (#374) + subtask-tree overlay show it; mirror here.
			// Error takes priority (a subtask has error OR result). First line + `…`
			// when there's more (multi-line or width-clamped), mirroring the tree.
			if (!st.error && st.result) {
				const rBudget = width !== undefined ? Math.max(0, width - indent.length - 2) : 60;
				const firstLine = st.result.split("\n")[0];
				const moreLines = st.result.includes("\n");
				const truncated = firstLine.length > rBudget - 1 || moreLines;
				const shown = truncated
					? firstLine.slice(0, Math.max(0, rBudget - 1)) + "…"
					: firstLine.slice(0, rBudget);
				lines.push(theme.fg("dim", `${indent}  ${shown}`));
			}
			if (st.retryCount && st.retryCount > 0) {
				lines.push(theme.fg("dim", `${indent}  Retries: ${st.retryCount}`));
			}
			// ponytail: review verdict — the subtask-tree overlay shows ✓/✗ approved/
			// rejected, but /uc status <id> + the task-list detail view (both fed by
			// formatTaskDetail) omitted it. A reviewed subtask's approval is the key
			// outcome; surface it on its own line, colored by verdict.
			if (st.review) {
				const label = st.review.approved ? "✓ approved" : "✗ rejected";
				lines.push(theme.fg(st.review.approved ? "success" : "error", `${indent}  Review: ${label}`));
				// ponytail: surface review issues/suggestions — a rejected subtask's
				// issues are the "why rejected" diagnostic the user most wants, but
				// the verdict line showed only ✓/✗. detail view has scroll, so each
				// issue/suggestion gets its own dim line (capped to width). Approved
				// reviews can still carry suggestions, so show both regardless of verdict.
				// `?? []`: older review records (and selfcheck stubs) may omit the arrays.
				const issues = st.review.issues ?? [];
				const suggestions = st.review.suggestions ?? [];
				if (width !== undefined) {
					const issueBudget = Math.max(0, width - indent.length - 4);
					for (const issue of issues) lines.push(theme.fg("dim", `${indent}    • ${cap(issue, issueBudget, 200)}`));
					for (const sug of suggestions) lines.push(theme.fg("dim", `${indent}    ↳ ${cap(sug, issueBudget, 200)}`));
				} else {
					for (const issue of issues) lines.push(theme.fg("dim", `${indent}    • ${cap(issue, 200, 200)}`));
					for (const sug of suggestions) lines.push(theme.fg("dim", `${indent}    ↳ ${cap(sug, 200, 200)}`));
				}
			}
		}
	}

	return lines;
}
