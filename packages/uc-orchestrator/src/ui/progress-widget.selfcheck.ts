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

// ponytail: multi-line or width-clamped stepSummary shows `…` — was a bare slice
// with no indicator, so a multi-line summary looked complete at line 1. Mirrors
// the subtask-tree result-ellipsis fix.
{
	const runningSt = { id: "s1", description: "work", status: "running", dependsOn: [], files: [] } as unknown as SubtaskResult;
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [runningSt] } as unknown as TaskState;
	const factory = createProgressWidget(() => ({ task, progressBySubtask: new Map([["s1", { phase: "executing", percent: 10, stepSummary: "line one\nline two" }]]) }));
	const comp = factory(undefined, theme) as any;
	const lines = comp.render(80) as string[];
	check("multi-line stepSummary shows first line + ellipsis", lines.some((l: string) => l.includes("line one") && l.includes("…")));

	// single-line that fits → NO ellipsis
	const factory2 = createProgressWidget(() => ({ task, progressBySubtask: new Map([["s1", { phase: "executing", percent: 10, stepSummary: "short step" }]]) }));
	const lines2 = (factory2(undefined, theme) as any).render(80) as string[];
	check("single-line stepSummary no ellipsis", lines2.some((l: string) => l.includes("short step") && !l.includes("…")));
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

// ponytail: paused task shows a resume affordance so the user doesn't have to
// open an overlay to discover how to resume. Only at width ≥ 60 (the ~38-char
// hint would overflow a narrow header). Cancelled/running show no hint.
{
	const paused = { id: "t1", description: "d", status: "paused", controlState: "paused", createdAt: 0, subtasks: [] } as unknown as TaskState;
	const st: ProgressWidgetState = { task: paused };
	const comp = createProgressWidget(() => st)(undefined, theme) as any;
	const wide = (comp.render(80) as string[])[0];
	check("paused wide shows resume affordance", wide.includes("/uc resume") && wide.includes("r in task list"));
	check("paused affordance names the overlay shortcut", wide.includes("Ctrl+Shift+T"));
	// ponytail: the affordance fills the ACTUAL (prefix-resolvable) id, not a `<id>`
	// placeholder — copy-paste runnable, no lookup needed.
	check("paused affordance shows the task id (not <id> placeholder)", wide.includes("/uc resume t1") && !wide.includes("<id>"));
	const narrow = (comp.render(40) as string[])[0];
	check("paused narrow omits affordance (no overflow)", !narrow.includes("/uc resume"));
}
// ponytail: a FAILED task shows the resume affordance too — it's resumable (resumeTask
// re-runs the failed wave), so the recovery path shouldn't hide behind opening an
// overlay. Mirrors the paused affordance exactly (same id + "r in task list"). Cancelled
// is terminal → no hint.
{
	const failed = { id: "t9", description: "d", status: "failed", controlState: "running", createdAt: 0, subtasks: [] } as unknown as TaskState;
	const st: ProgressWidgetState = { task: failed };
	const comp = createProgressWidget(() => st)(undefined, theme) as any;
	const wide = (comp.render(80) as string[])[0];
	check("failed wide shows resume affordance", wide.includes("/uc resume t9") && wide.includes("r in task list"));
	const cancelled = { id: "tC", description: "d", status: "cancelled", controlState: "running", createdAt: 0, subtasks: [] } as unknown as TaskState;
	const st2: ProgressWidgetState = { task: cancelled };
	const comp2 = createProgressWidget(() => st2)(undefined, theme) as any;
	const header2 = (comp2.render(80) as string[])[0];
	check("cancelled shows no resume affordance", !header2.includes("/uc resume"));
}
{
	const running = { id: "t2", description: "d", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [] } as unknown as TaskState;
	const st: ProgressWidgetState = { task: running };
	const comp = createProgressWidget(() => st)(undefined, theme) as any;
	const header = (comp.render(80) as string[])[0];
	check("running shows no resume affordance", !header.includes("/uc resume"));
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

// ponytail: F19 — elapsed tag from firstSeen (seeded at subtask_start, carried
// across progress updates); F20 — percent -1 (no data) renders no % tag.
{
	const lines65 = renderRunningWithProgress(
		{ phase: "executing", percent: 42, firstSeen: Date.now() - 65_000 }, 80);
	const progLine = lines65.find((l) => l.includes("42%")) ?? "";
	check("F19 elapsed tag rendered (1m)", progLine.includes("(1m"));

	const linesNoPct = renderRunningWithProgress(
		{ phase: "starting", percent: -1, firstSeen: Date.now() - 5_000 }, 80);
	check("F20 percent -1 renders no % tag", !linesNoPct.some((l) => l.includes("%")));
	check("F19 elapsed present even without percent", linesNoPct.some((l) => l.includes("(5s)")));

	// priority: under a tight budget parallel survives, elapsed drops (elapsed is
	// second-lowest priority — only phase trims/drops before it). width 22 →
	// budget 18: "42%" (3) + "↻3 parallel" (+12 = 15) fits, "(1m)" (+5 = 20) doesn't.
	const tight = renderRunningWithProgress({
		phase: "a very long phase name that will not fit", percent: 42,
		parallelGroup: "g1", parallelStepCount: 3, firstSeen: Date.now() - 65_000,
	}, 22);
	const tightLine = tight.find((l) => l.includes("parallel")) ?? "";
	check("F19 tight width keeps parallel", tightLine.length > 0);
	check("F19 tight width drops elapsed before parallel", !tightLine.includes("(1m"));
}

// ponytail: F24 — wave row: progress leads, wave identity trails (the bar
// counts task-wide completion — leading with "Wave X/Y" read as per-wave).
// Long LLM-chosen subtask ids budget by actual id length, not a fixed 12.
{
	const stW = { id: "s1", description: "w", status: "completed", dependsOn: [], files: [] } as unknown as SubtaskResult;
	const taskW = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [stW] } as unknown as TaskState;
	const wLines = (createProgressWidget(() => ({ task: taskW, waveIdx: 0, totalWaves: 2 }))(undefined, theme) as any).render(80) as string[];
	const waveLine = wLines.find((l) => l.includes("wave")) ?? "";
	check("F24 wave row: progress first, '· wave 1/2' trails", waveLine.includes("1/1") && waveLine.includes("· wave 1/2"));

	const longId = "implement-auth-subtask-with-a-very-long-id";
	const stL = { id: longId, description: "d".repeat(60), status: "running", dependsOn: [], files: [] } as unknown as SubtaskResult;
	const taskL = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [stL] } as unknown as TaskState;
	const lLines = (createProgressWidget(() => ({ task: taskL }))(undefined, theme) as any).render(50) as string[];
	const runLine = lLines.find((l) => l.includes(longId)) ?? "";
	check("F24 long-id running row fits width", runLine.length > 0 && runLine.length <= 50);
	// ponytail: a truncated running-row desc shows "…" (was a bare slice)
	check("F24 long-id running row desc shows ellipsis", runLine.includes("…"));
}

// ponytail: progress bar shows WITHOUT wave info — restored/resumed tasks (or
// single-wave tasks with missing wave data) had no bar despite completed/total
// being computable. Now the bar + count render whenever subtasks exist; the
// wave tag is optional trailing context.
{
	const st1 = { id: "s1", description: "d", status: "completed", dependsOn: [], files: [] } as unknown as SubtaskResult;
	const st2 = { id: "s2", description: "d", status: "pending", dependsOn: [], files: [] } as unknown as SubtaskResult;
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [st1, st2] } as unknown as TaskState;
	// no waveIdx/totalWaves — the pre-fix gate hid the bar entirely
	const lines = (createProgressWidget(() => ({ task }))(undefined, theme) as any).render(80) as string[];
	const barLine = lines.find((l) => l.includes("1/2")) ?? "";
	check("no-wave: progress bar + count shown", barLine.length > 0 && barLine.includes("1/2"));
	check("no-wave: no wave tag (none to show)", !barLine.includes("wave"));
	// zero subtasks → no bar row at all (no degenerate "0/0" line)
	const emptyTask = { ...task, subtasks: [] } as unknown as TaskState;
	const emptyLines = (createProgressWidget(() => ({ task: emptyTask }))(undefined, theme) as any).render(80) as string[];
	check("no-subtasks: no progress bar row", !emptyLines.some((l) => l.includes("0/0")));
}

// ponytail: failed subtasks render as a distinct (error-colored) segment, not
// empty ░ like pending. A task at 5/10 with 3 failed looked identical to one
// still running with 5 pending — the bar hid the failure. Now failed fills its
// own █ segment, so the bar shows MORE filled cells when subtasks failed.
// The theme mock strips ANSI, so detect via filled-vs-empty cell counts.
{
	// 5 completed, 3 failed, 2 pending — 10 total
	const mk = (id: string, status: string) => ({ id, description: "d", status, dependsOn: [], files: [] } as unknown as SubtaskResult);
	const taskWithFailed = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [mk("s0","completed"),mk("s1","completed"),mk("s2","completed"),mk("s3","completed"),mk("s4","completed"),
			mk("s5","failed"),mk("s6","failed"),mk("s7","failed"),mk("s8","pending"),mk("s9","pending")] } as unknown as TaskState;
	const linesW = (createProgressWidget(() => ({ task: taskWithFailed }))(undefined, theme) as any).render(80) as string[];
	// find the bar line (has both █ and ░, plus the 5/10 count)
	const barW = linesW.find((l) => l.includes("█") && l.includes("5/10")) ?? "";
	// 5 completed + 3 failed = 8 filled cells; 2 pending = 2 empty. No-fail case
	// (below) has only 5 filled. So filledW > filledNoFail proves the failed segment.
	const filledW = (barW.match(/█/g) ?? []).length;
	const emptyW = (barW.match(/░/g) ?? []).length;

	// control: same 5 completed, 0 failed, 5 pending → only 5 filled
	const mkC = (id: string) => ({ id, description: "d", status: "completed", dependsOn: [], files: [] } as unknown as SubtaskResult);
	const mkP = (id: string) => ({ id, description: "d", status: "pending", dependsOn: [], files: [] } as unknown as SubtaskResult);
	const taskNoFail = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [mkC("s0"),mkC("s1"),mkC("s2"),mkC("s3"),mkC("s4"),mkP("s5"),mkP("s6"),mkP("s7"),mkP("s8"),mkP("s9")] } as unknown as TaskState;
	const linesNF = (createProgressWidget(() => ({ task: taskNoFail }))(undefined, theme) as any).render(80) as string[];
	const barNF = linesNF.find((l) => l.includes("█") && l.includes("5/10")) ?? "";
	const filledNF = (barNF.match(/█/g) ?? []).length;

	check("failed bar: failed subtasks fill (more █ than no-fail control)", filledW > filledNF);
	check("failed bar: filled+empty = bar width (no overrun)", filledW > 0 && emptyW > 0 && (filledW + emptyW) === (barW.match(/[█░]/g) ?? []).length);
	check("failed bar: bar width stays within 30-col cap", (barW.match(/[█░]/g) ?? []).length <= 30);
	// ponytail: failTag next to the count — "5/10 ·3✗" (3 failed). No ✗ when 0 failed.
	check("failed bar: count text shows ·N✗ marker (3 failed)", barW.includes("5/10") && barW.includes("·3✗"));
	check("failed bar: no-fail control has no ✗ marker", !barNF.includes("✗"));
}

// ponytail: total run duration on a terminal task — "⏱ Ns" answers "how long
// did this run". Computed createdAt→completedAt. Only terminal w/ completedAt;
// in-flight shows the live age, no dur tag.
{
	const mk = (status: string, completedAt?: number) => ({
		task: { id: "T", description: "t", status, controlState: "running",
			createdAt: Date.now() - 60_000, completedAt, subtasks: [] } as unknown as TaskState,
	});
	const comp = createProgressWidget(() => mk("completed", Date.now() - 30_000))(undefined, theme) as any;
	const row = (comp.render(80) as string[]).find((l: string) => l.includes("UC")) ?? "";
	// createdAt=now-60s, completedAt=now-30s → ran 30s
	check("completed task shows ⏱ duration", row.includes("⏱") && row.includes("30s"));
	// failed task w/ completedAt → duration
	const fcomp = createProgressWidget(() => mk("failed", Date.now() - 30_000))(undefined, theme) as any;
	const frow = (fcomp.render(80) as string[]).find((l: string) => l.includes("UC")) ?? "";
	check("failed task shows ⏱ duration", frow.includes("⏱"));
	// in-flight → no dur tag
	const icomp = createProgressWidget(() => mk("in_progress", undefined))(undefined, theme) as any;
	const irow = (icomp.render(80) as string[]).find((l: string) => l.includes("UC")) ?? "";
	check("in_progress task no ⏱ duration", !irow.includes("⏱"));
}

// ponytail: idle widget hints /uc submit — the always-visible widget is a
// first-run user's first UC surface; "UC: idle" alone named no submit path
// (mirrors the overlay empty-state #422). Gated on width ≥ 50.
{
	const factory = createProgressWidget(() => null);
	const comp = factory(undefined, theme) as any;
	const wide = comp.render(80) as string[];
	check("idle widget shows /uc submit hint (wide)", wide.some((l: string) => l.includes("/uc submit")));
	const narrow = comp.render(40) as string[];
	check("idle widget no submit hint under 50 cols", !narrow.some((l: string) => l.includes("/uc submit")));
	check("idle widget still shows UC: idle", wide.some((l: string) => l.includes("UC: idle")));
}

// ponytail: header desc shows "…" when truncated — the slice was bare, so a long
// description cut silently. Mirrors the overlay rows (#449/#450).
{
	const mkState = (desc: string) => ({
		task: { id: "T", description: desc, status: "in_progress", controlState: "running", createdAt: 0, subtasks: [] } as unknown as TaskState,
	});
	// long desc at narrow width → truncated → "…"
	const comp = createProgressWidget(() => mkState("x".repeat(60)))(undefined, theme) as any;
	const row = (comp.render(30) as string[]).find((l: string) => l.includes("UC")) ?? "";
	check("widget truncated desc shows ellipsis", row.includes("…"));
	// short desc at wide width → no ellipsis
	const comp2 = createProgressWidget(() => mkState("short task"))(undefined, theme) as any;
	const row2 = (comp2.render(80) as string[]).find((l: string) => l.includes("UC")) ?? "";
	check("widget fitting desc no ellipsis", row2.includes("short task") && !row2.includes("…"));
}

// ponytail: running-count marker on the bar row — mirrors the subtask-tree/task-list
// headers (#458/#459). "·N▶" (accent) only when running>0; none when no running.
{
	const mkSub = (status: string) => ({ id: "s", description: "d", status, dependsOn: [], files: [] } as any);
	const task = (subs: any[]) => ({ id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: subs } as unknown as TaskState);
	// 2 running, 1 completed → bar "1/3 ·2▶" (completed=1 of 3)
	const comp = createProgressWidget(() => ({ task: task([mkSub("running"), mkSub("running"), mkSub("completed")]) }))(undefined, theme) as any;
	const bar = (comp.render(80) as string[]).find((l: string) => l.includes("1/3")) ?? "";
	check("bar row shows ·N▶ with running subtasks", bar.includes("·2▶"));
	// no running → no ▶ marker
	const comp2 = createProgressWidget(() => ({ task: task([mkSub("completed"), mkSub("pending")]) }))(undefined, theme) as any;
	const bar2 = (comp2.render(80) as string[]).find((l: string) => l.includes("/2")) ?? "";
	check("bar row no ▶ when no running subtasks", !bar2.includes("▶"));
}

// ponytail: blocked subtasks render a dim "⏳ N blocked: <ids>" line (mirrors the
// failed summary). A task with 0 running + N blocked showed nothing below the bar
// (only the ·N⏳ marker); the IDs tell which subtasks are stalled.
{
	const mk = (id: string, status: string, dependsOn: string[] = []) =>
		({ id, description: `d-${id}`, status, dependsOn, files: [] } as unknown as SubtaskResult);
	const task = (subs: any[]) => ({ id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: subs } as unknown as TaskState);
	// s3 pending + unmet dep (s1 pending) → blocked; s4 pending w/o deps → not blocked
	const subs = [mk("s1", "pending"), mk("s2", "completed"), mk("s3", "pending", ["s1"]), mk("s4", "pending")];
	const lines = (createProgressWidget(() => ({ task: task(subs) }))(undefined, theme) as any).render(80) as string[];
	const blockLine = lines.find((l) => l.includes("blocked:")) ?? "";
	check("blocked line lists blocked IDs", blockLine.includes("s3"));
	check("blocked line omits non-blocked", !blockLine.includes("s4"));
	check("blocked line fits width", blockLine.length <= 80);
}

// many blocked on a narrow terminal — truncate with ellipsis, fit width
{
	const mk = (i: number) => ({ id: `blocked-${i}`, description: "d", status: "pending", dependsOn: ["missing"], files: [] } as unknown as SubtaskResult);
	const subs = Array.from({ length: 12 }, (_, i) => mk(i));
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: subs } as unknown as TaskState;
	const lines = (createProgressWidget(() => ({ task }))(undefined, theme) as any).render(30) as string[];
	const blockLine = lines.find((l) => l.includes("blocked:")) ?? "";
	check("narrow: blocked line has ellipsis", blockLine.includes("…"));
	check("narrow: blocked line fits width", blockLine.length <= 30);
}

// no blocked subtasks → no blocked line
{
	const mk = (id: string, status: string, dependsOn: string[] = []) =>
		({ id, description: "d", status, dependsOn, files: [] } as unknown as SubtaskResult);
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [mk("s1", "completed"), mk("s2", "pending")] } as unknown as TaskState;
	const lines = (createProgressWidget(() => ({ task }))(undefined, theme) as any).render(80) as string[];
	check("no blocked subtasks → no blocked line", !lines.some((l: string) => l.includes("blocked:")));
}

// width so small only the prefix fits — no throw; no raw IDs beyond the prefix
{
	const mk = (id: string) => ({ id, description: "d", status: "pending", dependsOn: ["missing"], files: [] } as unknown as SubtaskResult);
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [mk("s1"), mk("s2")] } as unknown as TaskState;
	const lines = (createProgressWidget(() => ({ task }))(undefined, theme) as any).render(10) as string[];
	const blockLine = lines.find((l) => l.includes("blocked:")) ?? "";
	check("tiny width no throw", typeof blockLine === "string");
	check("tiny width appends no raw IDs", !blockLine.includes("s1"));
}

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
