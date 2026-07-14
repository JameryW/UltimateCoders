/**
 * Self-check for ProgressWidget failed-IDs truncation.
 * Run: bun run src/ui/progress-widget.selfcheck.ts
 *
 * ponytail: invariant — failed-ID list fits within terminal width, no wrap.
 */
import type { Theme, ThemeColor } from "@oh-my-pi/pi-coding-agent";
import type { TaskState, SubtaskResult } from "../orchestrator/orchestrator";
import { createProgressWidget, type ProgressWidgetState } from "./progress-widget";

const theme: Theme = {
	fg: (_c: ThemeColor, t: string) => t,
	bold: (t: string) => t,
} as unknown as Theme;

let failures = 0;
function check(name: string, cond: boolean) {
	console.log(`${cond ? "PASS" : "FAIL"}: ${name}`);
	if (!cond) failures++;
}

function failedSubtask(id: string): SubtaskResult {
	return {
		id, description: `d-${id}`, status: "failed", dependsOn: [], files: [],
		error: "boom",
	} as unknown as SubtaskResult;
}

function makeState(failedIds: string[]): ProgressWidgetState {
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks: failedIds.map(failedSubtask),
	} as unknown as TaskState;
	return { task };
}

function renderLines(failedIds: string[], width: number): string[] {
	let st = makeState(failedIds);
	const factory = createProgressWidget(() => st);
	const comp = factory(undefined, theme) as any;
	return comp.render(width) as string[];
}

// Few failures, wide terminal — all IDs present
{
	const lines = renderLines(["s1", "s2"], 80);
	const failedLine = lines.find((l) => l.includes("failed:")) ?? "";
	check("few failures list both IDs", failedLine.includes("s1") && failedLine.includes("s2"));
	check("few failures under width", failedLine.length <= 80);
}

// Many failures, narrow terminal — must truncate, fit width, keep ellipsis
{
	const ids = Array.from({ length: 20 }, (_, i) => `subtask-${i}`);
	const lines = renderLines(ids, 40);
	const failedLine = lines.find((l) => l.includes("failed:")) ?? "";
	check("many failures truncated (has ellipsis)", failedLine.includes("…"));
	check("many failures fits narrow width", failedLine.length <= 40);
}

// width so small only prefix fits — no throw; id list adds nothing beyond prefix
// (prefix `  ⚠ N failed: ` is ~14 cols, unavoidable floor)
{
	const lines = renderLines(["s1", "s2"], 10);
	const failedLine = lines.find((l) => l.includes("failed:")) ?? "";
	check("tiny width no throw", typeof failedLine === "string");
	// idBudget clamps to 0 → no raw IDs appended beyond the prefix
	check("tiny width appends no raw IDs", !failedLine.includes("s1"));
}

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
