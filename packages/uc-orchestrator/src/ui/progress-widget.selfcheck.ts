/**
 * Self-check for ProgressWidget failed-IDs truncation.
 * Run: bun run src/ui/progress-widget.selfcheck.ts
 *
 * ponytail: invariant — failed-ID list fits within terminal width, no wrap.
 */
import type { Theme, ThemeColor } from "@oh-my-pi/pi-coding-agent";
import type { TaskState, SubtaskResult } from "../orchestrator/orchestrator";
import { createProgressWidget, type ProgressWidgetState, type SubtaskProgressInfo } from "./progress-widget";

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

// ponytail: S9 — live-step tag line is width-aware. The joined line
// [agent, pct, step, status, parallel, phase] must never exceed `width`.
// On narrow terminals, phase is trimmed/dropped first (lowest priority);
// agent+pct always survive (highest priority). parallelTag/statusTag
// are kept before phase (dropped only if even they don't fit).
// selfcheck theme adds no ANSI, so string length == visible width.
function renderRunningWithProgress(prog: Record<string, unknown>, width: number): string[] {
	const runningSt = { id: "s1", description: "work", status: "running", dependsOn: [], files: [] } as unknown as SubtaskResult;
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [runningSt] } as unknown as TaskState;
	// ponytail: assert prog shape as SubtaskProgressInfo so the Map type matches
	// ProgressWidgetState.progressBySubtask (was Record<string,unknown> → TS2322).
	const progressBySubtask = new Map<string, SubtaskProgressInfo>([["s1", prog as unknown as SubtaskProgressInfo]]);
	const st: ProgressWidgetState = { task, progressBySubtask };
	const factory = createProgressWidget(() => st);
	const comp = factory(undefined, theme) as any;
	return comp.render(width) as string[];
}

// S9: wide terminal (80) — all tags fit, phase present, line <= 80
{
	const prog = {
		phase: "executing code edits", percent: 42, stepIndex: 3, stepTotal: 7,
		stepAgent: "coder", stepStatus: "retrying", parallelGroup: "g1", parallelStepCount: 3,
	};
	const lines = renderRunningWithProgress(prog, 80);
	const progLine = lines.find((l) => l.includes("coder")) ?? "";
	check("S9 w80: agent present", progLine.includes("coder"));
	check("S9 w80: pct present", progLine.includes("42%"));
	check("S9 w80: step present", progLine.includes("[3/7]"));
	check("S9 w80: status present", progLine.includes("[retry]"));
	check("S9 w80: parallel present", progLine.includes("parallel"));
	check("S9 w80: phase present", progLine.includes("executing"));
	check("S9 w80: line fits width", progLine.length <= 80);
}

// S9: medium terminal (40) — line <= 40, agent+pct survive, phase trimmed or absent
{
	const prog = {
		phase: "executing code edits in the auth module", percent: 42, stepIndex: 3, stepTotal: 7,
		stepAgent: "coder", stepStatus: "retrying", parallelGroup: "g1", parallelStepCount: 3,
	};
	const lines = renderRunningWithProgress(prog, 40);
	const progLine = lines.find((l) => l.includes("coder")) ?? "";
	check("S9 w40: agent present", progLine.includes("coder"));
	check("S9 w40: pct present", progLine.includes("42%"));
	check("S9 w40: line fits width", progLine.length <= 40);
	// phase either trimmed with ellipsis or absent
	const phaseOk = progLine.includes("…") || !progLine.includes("executing");
	check("S9 w40: phase trimmed or absent", phaseOk);
}

// S9: narrow terminal (20) — agent+pct survive (core), line <= 20
{
	const prog = {
		phase: "executing code edits", percent: 5, stepIndex: 1, stepTotal: 7,
		stepAgent: "coder", stepStatus: "failed", parallelGroup: "g1", parallelStepCount: 2,
	};
	const lines = renderRunningWithProgress(prog, 20);
	const progLine = lines.find((l) => l.includes("coder")) ?? "";
	check("S9 w20: agent present", progLine.includes("coder"));
	check("S9 w20: pct present", progLine.includes("5%"));
	check("S9 w20: line fits width", progLine.length <= 20);
}

// S9: existing live-step tests still pass (agent-only, pct-only, etc.)
{
	const runningSt = { id: "s1", description: "work", status: "running", dependsOn: [], files: [] } as unknown as SubtaskResult;
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [runningSt] } as unknown as TaskState;
	const progressBySubtask = new Map([["s1", { phase: "executing", percent: 42, stepIndex: 3, stepTotal: 7, stepAgent: "coder" }]]);
	let st: ProgressWidgetState = { task, progressBySubtask };
	const factory = createProgressWidget(() => st);
	const comp = factory(undefined, theme) as any;
	const lines = comp.render(80) as string[];
	const progLine = lines.find((l) => l.includes("coder")) ?? "";
	check("S9 legacy: percent rendered", progLine.includes("42%"));
	check("S9 legacy: stepIndex/stepTotal rendered", progLine.includes("[3/7]"));
	check("S9 legacy: agent tag rendered", progLine.includes("coder"));
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
