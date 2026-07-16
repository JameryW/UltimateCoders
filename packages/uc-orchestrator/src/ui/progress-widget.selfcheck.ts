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

// Live progress fields (percent + stepIndex/stepTotal) render, not dead data
// ponytail: SubtaskProgressInfo.percent/stepIndex/stepTotal were populated by the
// subtask_progress event but never displayed. Verify they now surface.
{
	const runningSt = { id: "s1", description: "work", status: "running", dependsOn: [], files: [] } as unknown as SubtaskResult;
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [runningSt] } as unknown as TaskState;
	const progressBySubtask = new Map([["s1", { phase: "executing", percent: 42, stepIndex: 3, stepTotal: 7, stepAgent: "coder" }]]);
	let st: ProgressWidgetState = { task, progressBySubtask };
	const factory = createProgressWidget(() => st);
	const comp = factory(undefined, theme) as any;
	const lines = comp.render(80) as string[];
	const progLine = lines.find((l) => l.includes("coder")) ?? "";
	check("percent rendered", progLine.includes("42%"));
	check("stepIndex/stepTotal rendered", progLine.includes("[3/7]"));
	check("agent tag rendered", progLine.includes("coder"));
}

// stepSummary renders on its own dim line (was dead data)
{
	const runningSt = { id: "s1", description: "work", status: "running", dependsOn: [], files: [] } as unknown as SubtaskResult;
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [runningSt] } as unknown as TaskState;
	const progressBySubtask = new Map([["s1", { phase: "executing", percent: 10, stepSummary: "editing auth.ts" }]]);
	let st: ProgressWidgetState = { task, progressBySubtask };
	const factory = createProgressWidget(() => st);
	const comp = factory(undefined, theme) as any;
	const lines = comp.render(80) as string[];
	check("stepSummary rendered", lines.some((l: string) => l.includes("editing auth.ts")));
}

// Header shows the task description (truncated to width) so the always-visible
// widget says what the task IS, not just a truncated UUID.
// ponytail: header was "UC <id> <status>" with no description.
{
	const task = {
		id: "task-uuid-1234", description: "Refactor the auth module into a service",
		status: "in_progress", controlState: "running", createdAt: 0, subtasks: [],
	} as unknown as TaskState;
	const st: ProgressWidgetState = { task };
	const comp = createProgressWidget(() => st)(undefined, theme) as any;
	const header = (comp.render(80) as string[])[0];
	check("header includes task description", header.includes("Refactor the auth module"));
	check("header still includes status", header.includes("in_progress"));
	check("header still includes UC marker", header.includes("UC"));
}

// Long description truncates to fit width (no overflow beyond terminal width)
{
	const task = {
		id: "t1", description: "x".repeat(200), status: "in_progress", controlState: "running",
		createdAt: 0, subtasks: [],
	} as unknown as TaskState;
	const st: ProgressWidgetState = { task };
	const comp = createProgressWidget(() => st)(undefined, theme) as any;
	const header = (comp.render(40) as string[])[0];
	// selfcheck theme adds no ANSI, so string length == visible width
	check("long desc header fits width", header.length <= 40);
	check("long desc header still has status", header.includes("in_progress"));
}

// Narrow terminal: description budget clamps to 0 -> header omits " - " (no overflow)
{
	const task = {
		id: "t1", description: "y".repeat(50), status: "in_progress", controlState: "running",
		createdAt: 0, subtasks: [],
	} as unknown as TaskState;
	const st: ProgressWidgetState = { task };
	const comp = createProgressWidget(() => st)(undefined, theme) as any;
	const header = (comp.render(20) as string[])[0];
	check("narrow terminal header omits description", !header.includes(" - "));
	check("narrow terminal header fits width", header.length <= 20);
}

// ponytail: S8 — failed subtask with retryCount > 0 shows a "retried N×" dim line.
// The retry count comes from SubtaskResult.retryCount (copied to st.retryCount by
// the orchestrator's result→TaskState copy path). formatErrorForDisplay reads
// st.error (pure root cause), so without this line the retry count would be
// invisible for remote/local subtasks whose error lacks the friendly prefix.
{
	const st = failedSubtask("s1");
	st.retryCount = 2;
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks: [st],
	} as unknown as TaskState;
	const progState: ProgressWidgetState = { task };
	const factory = createProgressWidget(() => progState);
	const comp = factory(undefined, theme) as any;
	const lines = comp.render(80) as string[];
	check("S8: retry line present", lines.some((l: string) => l.includes("retried")));
	check("S8: retry line shows count", lines.some((l: string) => l.includes("2")));
	check("S8: retry line has × symbol", lines.some((l: string) => l.includes("×")));
}

// ponytail: S8 — failed subtask with retryCount=0 (first attempt) does NOT show
// a retry line (no noise on first-attempt failures).
{
	const st = failedSubtask("s1");
	st.retryCount = 0;
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks: [st],
	} as unknown as TaskState;
	const progState: ProgressWidgetState = { task };
	const factory = createProgressWidget(() => progState);
	const comp = factory(undefined, theme) as any;
	const lines = comp.render(80) as string[];
	check("S8: retryCount=0 no retry line", !lines.some((l: string) => l.includes("retried")));
}

// ponytail: S8 — failed subtask with retryCount undefined (never retried, e.g.
// remote subtask where SubtaskProto has no retry_count) does NOT show retry line.
{
	const st = failedSubtask("s1");
	// retryCount left undefined (as-is from failedSubtask factory)
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks: [st],
	} as unknown as TaskState;
	const progState: ProgressWidgetState = { task };
	const factory = createProgressWidget(() => progState);
	const comp = factory(undefined, theme) as any;
	const lines = comp.render(80) as string[];
	check("S8: undefined retryCount no retry line", !lines.some((l: string) => l.includes("retried")));
}

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
