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
		const total = task.subtasks.length;
		const ctrl = task.controlState !== "running" ? ` [${task.controlState}]` : "";
		lines.push(`${icon} ${task.id.slice(0, 14)} ${completed}/${total} ${task.status}${ctrl}`);
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

	lines.push(`${icon} ${theme.bold(task.id)} — ${task.status}`);
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
			// ponytail: cap desc to the remaining width after indent+icon+prefix+id+deps.
			// Without width (legacy notify path), keep the 50-char cap.
			// F17: icon VISIBLE width is 1 — stIcon.length includes ~11 ANSI escape
			// chars in real terminals, so the old subtraction over-truncated desc
			// by ~10 chars (the no-ANSI selfcheck theme hid it).
			const headPlain = `${indent}${prefix}${st.id}: `;
			const descBudget = width !== undefined
				? Math.max(0, width - headPlain.length - 1 - 2 - depsPlain.length)
				: 50;
			lines.push(`${indent}${stIcon} ${prefix}${st.id}: ${cap(st.description, descBudget, 50)}${deps}`);

			if (st.error) {
				// ponytail: error budget = terminal width minus indent; default 60 for
				// the legacy notify path. formatErrorForDisplay slices plain error text
				// internally before theming, so passing a width is ANSI-safe.
				const errBudget = width !== undefined
					? Math.max(0, width - indent.length - 2)
					: 60;
				lines.push(`${indent}  ${formatErrorForDisplay(st.error, errBudget, (c, t) => theme.fg(c, t))}`);
			}
			if (st.retryCount && st.retryCount > 0) {
				lines.push(theme.fg("dim", `${indent}  Retries: ${st.retryCount}`));
			}
		}
	}

	return lines;
}
