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
	if (tasks.length === 0) return [theme.fg("dim", "No tasks")];

	const lines: string[] = [];
	for (const task of tasks) {
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
		const countTag = total > 0 ? `${completed}/${total} ${failTag}` : "";
		const ctrl = task.controlState !== "running" ? ` [${task.controlState}]` : "";
		lines.push(`${icon} ${task.id.slice(0, 14)} ${countTag}${task.status}${ctrl}`);
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
	const countTag = total > 0 ? ` ${completed}/${total}${failTag}` : "";
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
	lines.push(theme.fg("accent", "Subtasks:"));

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
		for (const st of subtasks) {
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
			const deps = depsPlain ? theme.fg("dim", depsPlain) : "";
			// ponytail: live elapsed on a running subtask — mirrors the subtask-tree
			// overlay's running-elapsed tag. /uc status <id> + the detail view showed a
			// running subtask with no time signal (only the tree overlay did). Built PLAIN
			// (before theming) so its length feeds the desc budget alongside depsPlain,
			// matching how the tree row subtracts the elapsed suffix.
			// A terminal subtask (completed/failed/cancelled) with completedAt shows
			// "(done Ns ago)" instead — the subtask-level mirror of the task-level doneTag
			// (#430). Running shows elapsed; terminal shows done-time; absent stamp → none.
			const timePlain = st.status === "running" && st.startedAt
				? ` (${formatElapsed(Date.now() - st.startedAt)})`
				: (st.completedAt && ["completed", "failed", "cancelled"].includes(st.status))
					? ` (done ${formatElapsed(Date.now() - st.completedAt)} ago)` : "";
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
			const descBudget = width !== undefined
				? Math.max(0, width - headPlain.length - 1 - 2 - depsPlain.length - timePlain.length - retryPlain.length)
				: 50;
			lines.push(`${indent}${stIcon} ${prefix}${st.id}: ${cap(st.description, descBudget, 50)}${deps}${elapsed}${retry}`);

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
