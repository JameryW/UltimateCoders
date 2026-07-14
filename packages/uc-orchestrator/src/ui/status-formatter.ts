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

export function formatTaskList(tasks: TaskState[], theme: Theme): string[] {
	if (tasks.length === 0) return [theme.fg("dim", "No tasks")];

	const lines: string[] = [];
	for (const task of tasks) {
		const icon = statusIcon(task.status, theme);
		const completed = task.subtasks.filter((s) => s.status === "completed").length;
		const total = task.subtasks.length;
		const ctrl = task.controlState !== "running" ? ` [${task.controlState}]` : "";
		lines.push(`${icon} ${task.id.slice(0, 14)} ${completed}/${total} ${task.status}${ctrl}`);
		lines.push(theme.fg("dim", `  ${task.description.slice(0, 60)}`));
	}
	return lines;
}

// ── Task Detail (with task ID) ───────────────────────────────────

export function formatTaskDetail(task: TaskState, theme: Theme): string[] {
	const lines: string[] = [];
	const icon = statusIcon(task.status, theme);

	lines.push(`${icon} ${theme.bold(task.id)} — ${task.status}`);
	lines.push(theme.fg("dim", `  Description: ${task.description}`));
	if (task.error) {
		lines.push(theme.fg("error", `  Error: ${task.error.slice(0, 100)}`));
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
			const deps = st.dependsOn.length > 0
				? theme.fg("dim", ` ←${st.dependsOn.join(",")}`)
				: "";
			lines.push(`${indent}${stIcon} ${prefix}${st.id}: ${st.description.slice(0, 50)}${deps}`);

			if (st.error) {
				lines.push(`${indent}  ${formatErrorForDisplay(st.error, 60, (c, t) => theme.fg(c, t))}`);
			}
			if (st.retryCount && st.retryCount > 0) {
				lines.push(theme.fg("dim", `${indent}  Retries: ${st.retryCount}`));
			}
		}
	}

	return lines;
}
