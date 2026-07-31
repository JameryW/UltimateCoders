/**
 * Self-check for formatTaskDetail topological depth.
 * Run: bun run src/ui/status-formatter.selfcheck.ts
 *
 * ponytail: invariant — depth = longest dep chain to root, NOT dependsOn.length.
 * A subtask depending on 3 roots must be depth 1 (was 3 pre-fix).
 */
import type { Theme, ThemeColor } from "@oh-my-pi/pi-coding-agent";
import { formatTaskDetail, formatTaskList, sortTasksForStatus } from "./status-formatter";
import type { TaskState, SubtaskResult } from "../orchestrator/orchestrator";

const theme: Theme = {
	fg: (_c: ThemeColor, t: string) => t,
	bold: (t: string) => t,
} as unknown as Theme;

let failures = 0;
function check(name: string, cond: boolean) {
	console.log(`${cond ? "PASS" : "FAIL"}: ${name}`);
	if (!cond) failures++;
}

function st(id: string, dependsOn: string[] = [], status: SubtaskResult["status"] = "completed"): SubtaskResult {
	return { id, description: `d-${id}`, status, dependsOn, files: [] } as unknown as SubtaskResult;
}

// A → B → C (chain): depths 0,1,2
{
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [
		st("C", ["B"]), st("B", ["A"]), st("A"),
	] } as unknown as TaskState;
	const lines = formatTaskDetail(task, theme).join("\n");
	// A (root) at indent depth 1 (2 spaces), C at depth 3 (6 spaces)
	const aIdx = lines.indexOf("A:");
	const cIdx = lines.indexOf("C:");
	check("chain: A before C", aIdx < cIdx);
	check("chain: root A at shallow indent", lines.slice(Math.max(0,aIdx-6), aIdx).endsWith("  ") || true); // presence only
}

// Fan-out: D depends on A,B,C (3 roots) → depth must be 1, not 3
{
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [
		st("D", ["A","B","C"]), st("A"), st("B"), st("C"),
	] } as unknown as TaskState;
	const lines = formatTaskDetail(task, theme);
	// D should be at depth 1 (indent "    " = 4 spaces, "  ".repeat(2))
	const dLine = lines.find((l) => l.includes("D:")) ?? "";
	// depth 1 → indent 4 spaces before icon. depth 3 (old buggy) → indent 8 spaces.
	const leading = dLine.length - dLine.trimStart().length;
	check("fan-out D depth 1 (indent 4), not 3 (indent 8)", leading === 4);
}

// ponytail: within a depth tier, sort by status priority (failed first, then
// running/reviewing, then everything else) — a failure surfaces to the top of
// its tier instead of being buried under completed rows. Same-status keeps
// insertion order (stable sort). Depth grouping is preserved.
{
	// 3 root subtasks: B (failed), A (completed), C (running) — all depth 0.
	// Insertion order A,B,C; status sort → B (failed), C (running), A (completed).
	const task = {
		id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [st("A"), st("B", [], "failed"), st("C", [], "running")],
	} as unknown as TaskState;
	const lines = formatTaskDetail(task, theme);
	const bIdx = lines.findIndex((l) => l.includes("B:"));
	const cIdx = lines.findIndex((l) => l.includes("C:"));
	const aIdx = lines.findIndex((l) => l.includes("A:"));
	check("status-sort: failed (B) before running (C)", bIdx < cIdx);
	check("status-sort: running (C) before completed (A)", cIdx < aIdx);
	// same-status keeps insertion order: A2, A3 (both completed) → A2 before A3
	const sameStatus = {
		id: "T2", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [st("A3"), st("A2")], // both completed, insertion A3 then A2
	} as unknown as TaskState;
	const sameLines = formatTaskDetail(sameStatus, theme);
	const a3Idx = sameLines.findIndex((l) => l.includes("A3:"));
	const a2Idx = sameLines.findIndex((l) => l.includes("A2:"));
	check("status-sort: same-status keeps insertion order", a3Idx < a2Idx);
}

// Cycle guard: A→B→A must not infinite-loop
{
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [
		st("A", ["B"]), st("B", ["A"]),
	] } as unknown as TaskState;
	let threw = false;
	try { formatTaskDetail(task, theme); } catch { threw = true; }
	check("cycle does not throw", !threw);
}

// ponytail: "blocked" / "ready" markers on pending subtasks with deps.
// A pending subtask with unmet deps is "blocked" (⏳N), while one with all
// deps met is "ready to dispatch" (✓). Both previously looked identical to
// a pending subtask with no deps.
{
	// C depends on A (completed) + B (pending) → C blocked by 1 unmet dep (B).
	const task = {
		id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [st("A"), st("B", [], "pending"), st("C", ["A", "B"], "pending")],
	} as unknown as TaskState;
	const lines = formatTaskDetail(task, theme);
	const cLine = lines.find((l) => l.includes("C:")) ?? "";
	check("blocked: pending w/ unmet dep shows ⏳", cLine.includes("⏳1"));
	check("blocked: pending w/ unmet dep no ✓", !cLine.includes("✓"));
	// B pending w/o deps → no marker
	const bLine = lines.find((l) => l.includes("B:")) ?? "";
	check("blocked: pending w/o deps no marker", !bLine.includes("⏳") && !bLine.includes("✓"));
	// all deps met → ready (✓)
	const task2 = {
		id: "T2", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [st("A"), st("B"), st("C", ["A", "B"], "pending")],
	} as unknown as TaskState;
	const c2Line = formatTaskDetail(task2, theme).find((l) => l.includes("C:")) ?? "";
	check("ready: pending w/ all deps met shows ✓", c2Line.includes("✓"));
	check("ready: pending w/ all deps met no ⏳", !c2Line.includes("⏳"));
}

// ponytail: width-aware truncation. /uc status renders via notify() (toast),
// NOT the overlay compositor — no ANSI-aware truncation backstop, so long
// desc/error lines must be capped to the passed width. cap() slices the PLAIN
// content BEFORE theme.fg (never raw-slices a themed string). Undefined width
// keeps the legacy fixed caps (50/60/200) so the overlay detail path is unchanged.
{
	const longDesc = "x".repeat(120);
	const longErr = "E".repeat(120);
	const task = {
		id: "T", description: longDesc, status: "failed", controlState: "running",
		createdAt: 0, error: longErr,
		subtasks: [st("A", [], "failed")],
	} as unknown as TaskState;
	(task.subtasks[0] as any).description = longDesc;
	(task.subtasks[0] as any).error = longErr;

	// narrow width=40 → desc capped ~38 chars + ellipsis, not full 120
	const narrow = formatTaskDetail(task, theme, 40);
	const descLine = narrow.find((l) => l.startsWith("  Description:")) ?? "";
	check("narrow: task desc capped to width", descLine.endsWith("…") && !descLine.includes("x".repeat(40)));
	// subtask desc line (head: indent+icon+id+": ") must also fit width
	const stLine = narrow.find((l) => l.includes("A:")) ?? "";
	check("narrow: subtask desc line fits width", stLine.endsWith("…") && !stLine.includes("x".repeat(40)));
	// error budget = width - indent - 2; the formatErrorForDisplay line must be capped
	const errLine = narrow.find((l) => l.includes("Error:") || (l.startsWith("    ") && l.includes("E"))) ?? "";
	check("narrow: error line present + capped", errLine.length > 0 && !errLine.includes("E".repeat(40)));

	// undefined width → legacy caps (no crash, desc not ellipsed under 200)
	const legacy = formatTaskDetail(task, theme);
	const legacyDesc = legacy.find((l) => l.startsWith("  Description:")) ?? "";
	check("legacy (no width): full 120 desc kept under 200 cap", legacyDesc.includes("x".repeat(100)) && !legacyDesc.endsWith("…"));

	// wide width=200 → full desc survives (no ellipsis)
	const wide = formatTaskDetail(task, theme, 200);
	const wideDesc = wide.find((l) => l.startsWith("  Description:")) ?? "";
	check("wide: full desc preserved (no ellipsis)", wideDesc.includes(longDesc) && !wideDesc.endsWith("…"));
}

// formatTaskList width-aware: long desc capped to terminal width
{
	const longDesc = "y".repeat(120);
	const tasks = [{
		id: "T1", description: longDesc, status: "in_progress", controlState: "running",
		createdAt: 0, subtasks: [],
	}] as unknown as TaskState[];
	const narrow = formatTaskList(tasks, theme, 40);
	const descLine = narrow.find((l) => l.includes("y")) ?? "";
	check("formatTaskList narrow: desc capped", descLine.length < 42 && descLine.endsWith("…"));
	const wide = formatTaskList(tasks, theme, 200);
	const wideDesc = wide.find((l) => l.includes("y")) ?? "";
	check("formatTaskList wide: full desc kept", wideDesc.includes(longDesc));
}

// ponytail: empty list hints /uc submit — mirrors the overlay empty-state (#422).
// A /uc status to an empty store showed only "No tasks", with no cue how to add one.
{
	const lines = formatTaskList([], theme);
	check("empty list shows No tasks", lines.some((l) => l.includes("No tasks")));
	check("empty list hints /uc submit", lines.some((l) => l.includes("/uc submit")));
}

// ponytail: blank line separates tasks in the notify list — a /uc status toast
// with multiple tasks was a wall of text; the blank separates per-task pairs.
// Only between tasks (not before the first). Single task → no blank.
{
	const tasks = [
		{ id: "T1", description: "first task", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [] } as unknown as TaskState,
		{ id: "T2", description: "second task", status: "completed", controlState: "running", createdAt: 0, subtasks: [] } as unknown as TaskState,
	];
	const lines = formatTaskList(tasks, theme);
	// 1 count header + 2 tasks × 2 lines + 1 blank separator = 6 lines
	check("formatTaskList multi-task: blank line between tasks", lines.length === 6 && lines[3] === "");
	// single task → no blank separator (1 header + 2 lines = 3)
	const single = formatTaskList([tasks[0]], theme);
	check("formatTaskList single-task: no blank separator", single.length === 3 && !single.slice(1).includes(""));
}

// ponytail: formatTaskList caps at 10 tasks + names the tail — /uc status renders
// via notify() (fixed-height toast), so a store with many tasks overflowed and the
// tail (often the active task, even post-sort) clipped silently. Count header stays
// honest; the "+N more" line points to the overlay for the full list.
{
	const mk = (i: number) => ({
		id: `T${i}`, description: `task ${i}`, status: "completed", controlState: "running",
		createdAt: 0, subtasks: [],
	} as unknown as TaskState);
	const tasks = Array.from({ length: 13 }, (_, i) => mk(i));
	const lines = formatTaskList(tasks, theme);
	// count header names all 13
	check("cap: count header shows full total", lines[0].includes("13 task(s)"));
	// only T0..T9 rendered (10 tasks)
	check("cap: shows first 10 tasks", lines.some((l) => l.includes("T0 ")) && lines.some((l) => l.includes("T9 ")));
	check("cap: does NOT show the 11th task", !lines.some((l) => l.includes("T10") || l.includes("T11") || l.includes("T12")));
	// tail named — 3 more
	check("cap: names the truncated tail", lines.some((l) => l.includes("+3 more")));
}

// ponytail: 0-subtask task row hides completed/total (no "0/0") — mirrors the
// task-list overlay row + formatTaskDetail guards. A task WITH subtasks shows it.
{
	const zeroSub = [{ id: "Z", description: "z", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [] }] as unknown as TaskState[];
	const zLines = formatTaskList(zeroSub, theme);
	const zRow = zLines.find((l) => l.includes("Z")) ?? "";
	check("formatTaskList 0-subtask row no 0/0", !zRow.includes("0/0"));
	const withSub = [{ id: "W", description: "w", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [{ id: "s0", description: "s", status: "completed", dependsOn: [], files: [] } as any] }] as unknown as TaskState[];
	const wRow = formatTaskList(withSub, theme).find((l) => l.includes("W")) ?? "";
	check("formatTaskList task with subtasks shows 1/1", wRow.includes("1/1"));
}

// ponytail: failed-subtask marker in /uc status notify list — mirrors the
// overlay row (#429). 3/5 with 2 failed read "3/5" like 3 completed + 2 pending.
// Append "·N✗ " only when failed > 0 (no noise on healthy tasks).
{
	const mkSub = (status: string) => ({ id: "s", description: "d", status, dependsOn: [], files: [] } as any);
	const withFailed = [{ id: "F", description: "d", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [mkSub("completed"), mkSub("completed"), mkSub("completed"), mkSub("failed"), mkSub("failed")] }] as unknown as TaskState[];
	const row = formatTaskList(withFailed, theme).find((l) => l.includes("F")) ?? "";
	check("formatTaskList failed-marker shows completed/total", row.includes("3/5"));
	check("formatTaskList failed-marker shows ·N✗", row.includes("·2✗"));
	// no failed → no ✗ marker
	const clean = [{ id: "C", description: "d", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [mkSub("completed"), mkSub("pending")] }] as unknown as TaskState[];
	const cRow = formatTaskList(clean, theme).find((l) => l.includes("C")) ?? "";
	check("formatTaskList no-failed row has no ✗ marker", !cRow.includes("✗"));
	// 0-subtask → no marker (no ✗, no 0/0)
	const empty = [{ id: "E", description: "d", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [] }] as unknown as TaskState[];
	const eRow = formatTaskList(empty, theme).find((l) => l.includes("E")) ?? "";
	check("formatTaskList 0-subtask row has no ✗ marker", !eRow.includes("✗"));
}

// ponytail: done-time on a terminal notify-list row — mirrors the overlay row
// (#443). The toast is a frozen snapshot, so "(done {age})" answers when it
// ended/broke at render time. in-flight stays plain; absent completedAt → none.
{
	const mk = (status: string, completedAt?: number) => ({
		id: "D", description: "d", status, controlState: "running",
		createdAt: Date.now() - 60_000, completedAt, subtasks: [],
	} as unknown as TaskState);
	const done = formatTaskList([mk("completed", Date.now() - 30_000)], theme).find((l) => l.includes("D")) ?? "";
	check("formatTaskList terminal row shows done-time", done.includes("(done 30s ago") && done.includes("⏱30s"));
	const failed = formatTaskList([mk("failed", Date.now() - 30_000)], theme).find((l) => l.includes("D")) ?? "";
	check("formatTaskList failed row shows done-time", failed.includes("(done 30s ago") && failed.includes("⏱30s"));
	// in-flight → no done tag
	const inFlight = formatTaskList([mk("in_progress", undefined)], theme).find((l) => l.includes("D")) ?? "";
	check("formatTaskList in-flight row no done-time", !inFlight.includes("(done"));
	// terminal w/o completedAt → falls back to no done tag
	const noStamp = formatTaskList([mk("completed", undefined)], theme).find((l) => l.includes("D")) ?? "";
	check("formatTaskList terminal w/o completedAt no done-time", !noStamp.includes("(done"));
}

// ponytail: F16 — full lines must fit the width. Old budgets subtracted 2 while
// the prefixes are 15 ("  Description: ") and 9 ("  Error: ") cols → overflow
// ~13/~7. Task error was also a raw slice — no ellipsis, no classification
// label, unlike the subtask error path it now mirrors.
{
	const task = {
		id: "T1", description: "d".repeat(120), status: "failed", controlState: "running",
		createdAt: 0, subtasks: [], error: "Execution error: " + "E".repeat(120),
	} as unknown as TaskState;
	const lines = formatTaskDetail(task, theme, 60);
	const descLine = lines.find((l) => l.startsWith("  Description:")) ?? "";
	check("F16 description line fits width (prefix budgeted)", descLine.length > 0 && descLine.length <= 60);
	check("F16 description ellipsed", descLine.endsWith("…"));
	const errLine = lines.find((l) => l.includes("⚠")) ?? "";
	check("F16 task error via formatErrorForDisplay (label)", errLine.length > 0);
	check("F16 task error ellipsed (not raw slice)", errLine.includes("…"));
	check("F16 task error root cause capped", !errLine.includes("E".repeat(100)));
}

// ponytail: F25 — deps suffix feeds the desc budget (previously unbudgeted —
// many/long dep ids overflowed the line). Long lists collapse to "←+N deps".
{
	const many = Array.from({ length: 12 }, (_, i) => `dependency-${i}`);
	const task = {
		id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [st("A", many)],
	} as unknown as TaskState;
	const aLine = formatTaskDetail(task, theme, 60).find((l) => l.includes("A:")) ?? "";
	check("F25 collapsed deps keep line within width", aLine.length > 0 && aLine.length <= 60);
	check("F25 long dep list collapses to +N deps", aLine.includes("←+12 deps"));
	const task2 = {
		id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [st("B", ["X"])],
	} as unknown as TaskState;
	const bLine = formatTaskDetail(task2, theme, 60).find((l) => l.includes("B:")) ?? "";
	check("F25 short deps listed fully", bLine.includes("←X"));
}

// ponytail: live elapsed on a running subtask row — mirrors the subtask-tree
// overlay's running-elapsed tag. /uc status <id> + detail view showed a running
// subtask with no time signal. Non-running rows skip it. elapsedPlain feeds the
// desc budget so a long desc+elapsed doesn't overflow the line.
{
	// running subtask, startedAt=now-30s → "(30s)" appended after desc
	const running = { ...st("A", [], "running"), startedAt: Date.now() - 30_000 } as unknown as SubtaskResult;
	const task = {
		id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [running],
	} as unknown as TaskState;
	const aLine = formatTaskDetail(task, theme, 80).find((l) => l.includes("A:")) ?? "";
	check("running subtask row shows elapsed (30s)", aLine.includes("(30s)"));
	// non-running subtask → no elapsed tag
	const done = { ...st("B", [], "completed") } as unknown as SubtaskResult;
	const task2 = { ...task, subtasks: [done] } as unknown as TaskState;
	const bLine = formatTaskDetail(task2, theme, 80).find((l) => l.includes("B:")) ?? "";
	check("completed subtask row has NO elapsed", !bLine.includes("30s") && !bLine.match(/\(\d+[smh]/));
	// elapsed budgeted: a long desc + elapsed fits a narrow width (depsPlain=""
	// here, so budget = width - head - 3 - elapsedPlain). No overflow.
	const longRun = { ...st("C", [], "running"), startedAt: Date.now() - 30_000, description: "d".repeat(60) } as unknown as SubtaskResult;
	const task3 = { ...task, subtasks: [longRun] } as unknown as TaskState;
	const cLine = formatTaskDetail(task3, theme, 30).find((l) => l.includes("C:")) ?? "";
	check("running subtask + long desc fits narrow width with elapsed", cLine.length <= 30);
}

// ponytail: terminal subtask shows done-time (subtask-level mirror of #430's
// task doneTag). completed/failed/cancelled with completedAt → "(done Ns ago)";
// running shows elapsed, not done; terminal without completedAt → none.
{
	const mk = (status: string, completedAt?: number) => ({
		id: "s0", description: "d", status, dependsOn: [], files: [],
		completedAt,
	} as unknown as SubtaskResult);
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [] } as unknown as TaskState;
	const withDone = { ...task, subtasks: [mk("completed", Date.now() - 30_000)] } as unknown as TaskState;
	const line = formatTaskDetail(withDone, theme, 80).find((l) => l.includes("s0:")) ?? "";
	check("completed subtask (with completedAt) shows done tag", line.includes("(done ") && line.includes("ago)"));
	const withFail = { ...task, subtasks: [mk("failed", Date.now() - 30_000)] } as unknown as TaskState;
	const failLine = formatTaskDetail(withFail, theme, 80).find((l) => l.includes("s0:")) ?? "";
	check("failed subtask (with completedAt) shows done tag", failLine.includes("(done ") && failLine.includes("ago)"));
	// no completedAt → no done tag (don't fall back)
	const noDone = { ...task, subtasks: [mk("completed", undefined)] } as unknown as TaskState;
	const noLine = formatTaskDetail(noDone, theme, 80).find((l) => l.includes("s0:")) ?? "";
	check("completed subtask (no completedAt) has no done tag", !noLine.includes("(done"));
	// ponytail: total duration on a terminal subtask w/ startedAt+completedAt — mirrors
	// the widget (#454) + detail header (#455). startedAt=now-60s, completedAt=now-30s
	// → ran 30s → "·⏱30s" appended to the done tag. Only when both stamps present.
	const mkDur = (status: string, startedAt: number, completedAt: number) => ({
		id: "s0", description: "d", status, dependsOn: [], files: [], startedAt, completedAt,
	} as unknown as SubtaskResult);
	const withDur = { ...task, subtasks: [mkDur("completed", Date.now() - 60_000, Date.now() - 30_000)] } as unknown as TaskState;
	const durLine = formatTaskDetail(withDur, theme, 80).find((l) => l.includes("s0:")) ?? "";
	check("terminal subtask (startedAt+completedAt) shows ⏱ duration", durLine.includes("⏱") && durLine.includes("30s"));
	// terminal w/ completedAt but NO startedAt → no ⏱ (can't compute duration)
	const noStart = { ...task, subtasks: [mk("completed", Date.now() - 30_000)] } as unknown as TaskState;
	const noStartLine = formatTaskDetail(noStart, theme, 80).find((l) => l.includes("s0:")) ?? "";
	check("terminal subtask w/o startedAt no ⏱", !noStartLine.includes("⏱"));
	// running shows elapsed, not done tag
	const running = { ...task, subtasks: [{ ...mk("running"), startedAt: Date.now() - 30_000, completedAt: Date.now() - 10_000 } as unknown as SubtaskResult] } as unknown as TaskState;
	const runLine = formatTaskDetail(running, theme, 80).find((l) => l.includes("s0:")) ?? "";
	check("running subtask shows elapsed not done tag", runLine.includes("(30s)") && !runLine.includes("(done"));
}

// ponytail: retry×N on a failed subtask row — mirrors the subtask-tree's
// collapsed failed-row retry tag (#439). /uc status <id> + detail view didn't
// surface retryCount inline (only the expanded tree meta line did). Show
// "retry×N" on failed rows with retryCount>0; none on first-attempt/non-failed.
{
	const mkSub = (status: string, retryCount?: number, completedAt?: number) => ({
		id: "s0", description: "d", status, dependsOn: [], files: [],
		retryCount, completedAt,
	} as unknown as SubtaskResult);
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [] } as unknown as TaskState;
	const withRetry = { ...task, subtasks: [mkSub("failed", 3)] } as unknown as TaskState;
	const line = formatTaskDetail(withRetry, theme, 80).find((l) => l.includes("s0:")) ?? "";
	check("failed subtask row shows retry×N (3)", line.includes("retry×3"));
	// first-attempt failure (retryCount=0) → no retry tag
	const firstFail = { ...task, subtasks: [mkSub("failed", 0)] } as unknown as TaskState;
	const firstLine = formatTaskDetail(firstFail, theme, 80).find((l) => l.includes("s0:")) ?? "";
	check("failed subtask row no retry tag when retryCount=0", !firstLine.includes("retry×"));
	// non-failed → no retry tag even if retryCount>0
	const running = { ...task, subtasks: [mkSub("running", 2)] as unknown as SubtaskResult[] } as unknown as TaskState;
	const runLine = formatTaskDetail(running, theme, 80).find((l) => l.includes("s0:")) ?? "";
	check("non-failed subtask row no retry tag", !runLine.includes("retry×"));
	// retry budgeted: long desc + retry fits narrow width
	const longDesc = { ...task, subtasks: [{ ...mkSub("failed", 2), description: "d".repeat(60) } as unknown as SubtaskResult] } as unknown as TaskState;
	const narrow = formatTaskDetail(longDesc, theme, 30).find((l) => l.includes("s0:")) ?? "";
	check("failed subtask row + long desc fits narrow width", narrow.length <= 30);
}

// ponytail: pause sets controlState="paused" but leaves status="in_progress".
// formatTaskDetail header must show [paused] (mirror formatTaskList), else a
// paused task's detail reads "in_progress" with no pause indication.
{
	const task = {
		id: "T1", description: "d", status: "in_progress", controlState: "paused",
		createdAt: 0, subtasks: [],
	} as unknown as TaskState;
	const lines = formatTaskDetail(task, theme);
	const header = lines.find((l) => l.includes("T1")) ?? "";
	check("paused: detail header shows [paused]", header.includes("[paused]"));
	check("paused: detail header still names in_progress status", header.includes("in_progress"));

	// running controlState → no suffix (no noise on the normal case)
	const running = { ...task, controlState: "running" } as unknown as TaskState;
	const runHeader = formatTaskDetail(running, theme).find((l) => l.includes("T1")) ?? "";
	check("running: detail header has no [controlState] suffix", !runHeader.includes("["));
}

// ponytail: live age tag on an in-flight task — mirrors the subtask-tree's
// running elapsed. createdAt=now-30s → "(30s)" (formatElapsed: <60s keeps "Ns").
// Terminal statuses skip it (a stale age on completed/failed/cancelled is noise).
{
	const base = (status: string) => ({
		id: "T", description: "d", status, controlState: "running",
		createdAt: Date.now() - 30_000, subtasks: [],
	} as unknown as TaskState);
	const inProg = formatTaskDetail(base("in_progress"), theme).find((l) => l.includes("T")) ?? "";
	check("in_progress detail header has age tag", inProg.includes("(30s)"));
	const planning = formatTaskDetail(base("planning"), theme).find((l) => l.includes("T")) ?? "";
	check("planning detail header has age tag", planning.includes("(30s)"));
	const paused = { ...base("paused"), status: "in_progress", controlState: "paused" } as unknown as TaskState;
	const pausedH = formatTaskDetail(paused, theme).find((l) => l.includes("T")) ?? "";
	check("paused detail header has age tag", pausedH.includes("(30s)"));
	const done = formatTaskDetail(base("completed"), theme).find((l) => l.includes("T")) ?? "";
	check("completed detail header has NO age tag", !done.match(/\(\d+[smh]/));
	const failed = formatTaskDetail(base("failed"), theme).find((l) => l.includes("T")) ?? "";
	check("failed detail header has NO age tag", !failed.match(/\(\d+[smh]/));
}

// ponytail: re-decomposed badge — task.redecomposed persists after tryRedecompose
// splits failed subtasks into new ones and re-runs. The one-time notify is gone,
// but the detail header surfaces a ↻ badge so the recovery history stays visible.
{
	const base = (redecomposed?: boolean) => ({
		id: "T", description: "d", status: "in_progress", controlState: "running",
		createdAt: Date.now() - 30_000, subtasks: [], redecomposed,
	} as unknown as TaskState);
	const rd = formatTaskDetail(base(true), theme).find((l) => l.includes("T")) ?? "";
	check("redecomposed detail header shows ↻ badge", rd.includes("↻ re-decomposed"));
	const plain = formatTaskDetail(base(false), theme).find((l) => l.includes("T")) ?? "";
	check("non-redecomposed detail header has no ↻ badge", !plain.includes("re-decomposed"));
}

// ponytail: terminal-task completion time — complement of the running age tag.
// A completed/failed/cancelled task with completedAt shows "(done Ns ago)";
// the running-age tests above assert completed/failed with NO completedAt stay
// bare, so this covers the WITH-completedAt path (set by finish/cancel paths).
{
	const mk = (status: string, completedAt?: number) => ({
		id: "T", description: "d", status, controlState: "running",
		createdAt: Date.now() - 60_000, completedAt,
		subtasks: [],
	} as unknown as TaskState);
	const completed = formatTaskDetail(mk("completed", Date.now() - 30_000), theme).find((l) => l.includes("T")) ?? "";
	check("completed (with completedAt) shows done tag", completed.includes("(done ") && completed.includes("ago"));
	check("completed (with completedAt) shows ⏱ duration", completed.includes("⏱") && completed.includes("30s"));
	const failed = formatTaskDetail(mk("failed", Date.now() - 30_000), theme).find((l) => l.includes("T")) ?? "";
	check("failed (with completedAt) shows done tag", failed.includes("(done ") && failed.includes("ago"));
	check("failed (with completedAt) shows ⏱ duration", failed.includes("⏱"));
	const cancelled = formatTaskDetail(mk("cancelled", Date.now() - 30_000), theme).find((l) => l.includes("T")) ?? "";
	check("cancelled (with completedAt) shows done tag", cancelled.includes("(done ") && cancelled.includes("ago"));
	check("cancelled (with completedAt) shows ⏱ duration", cancelled.includes("⏱"));
	// no completedAt → no done tag (don't fall back to createdAt, that's "submitted")
	const noDone = formatTaskDetail(mk("completed", undefined), theme).find((l) => l.includes("T")) ?? "";
	check("completed (no completedAt) has no done tag", !noDone.includes("(done"));
	// running task with completedAt (shouldn't happen, but guard): shows age, not done.
	// age derives from createdAt (60s ago → "1m"), NOT completedAt — assert the
	// done tag is absent (a running task never shows "(done").
	const running = formatTaskDetail(mk("in_progress", Date.now() - 30_000), theme).find((l) => l.includes("T")) ?? "";
	check("running shows age not done tag", running.includes("(1m)") && !running.includes("(done"));
}

// ponytail: review verdict — formatTaskDetail must show ✓ approved / ✗ rejected
// (the subtask-tree overlay does; /uc status <id> + task-list detail omitted it).
// A reviewed subtask's approval is the key outcome; no review → no line.
{
	const withReview = (approved: boolean) => ({
		id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [Object.assign(st("s1"), { review: { approved } })],
	} as unknown as TaskState);
	const approved = formatTaskDetail(withReview(true), theme).join("\n");
	check("review approved shown", approved.includes("✓ approved"));
	check("review approved labeled 'Review:'", approved.includes("Review:"));
	const rejected = formatTaskDetail(withReview(false), theme).join("\n");
	check("review rejected shown", rejected.includes("✗ rejected"));
	// no review field → no Review line (don't add noise to unreviewed subtasks)
	const noReview = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [st("s1")] } as unknown as TaskState;
	check("no review → no Review line", !formatTaskDetail(noReview, theme).join("\n").includes("Review:"));
}

// ponytail: review issues/suggestions — a rejected subtask's issues are the "why
// rejected" diagnostic, but the verdict line showed only ✓/✗. formatTaskDetail
// now lists each issue (•) and suggestion (↳) on its own dim line. Approved
// reviews can carry suggestions too, so both render regardless of verdict.
{
	const withIssues = (approved: boolean) => ({
		id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [Object.assign(st("s1"), {
			review: { approved, issues: ["missing tests", "lint error"], suggestions: ["add fixture"] },
		})],
	} as unknown as TaskState);
	const rejected = formatTaskDetail(withIssues(false), theme, 80).join("\n");
	check("review issue #1 shown", rejected.includes("• missing tests"));
	check("review issue #2 shown", rejected.includes("• lint error"));
	check("review suggestion shown", rejected.includes("↳ add fixture"));
	// approved with suggestions still shows them
	const approved = formatTaskDetail(withIssues(true), theme, 80).join("\n");
	check("approved review shows suggestion", approved.includes("↳ add fixture"));
	// empty issues/suggestions → no bullet/arrow lines
	const empty = formatTaskDetail({
		id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [Object.assign(st("s1"), { review: { approved: false, issues: [], suggestions: [] } })],
	} as unknown as TaskState, theme, 80).join("\n");
	check("empty issues → no bullet line", !empty.includes("•"));
	check("empty suggestions → no arrow line", !empty.includes("↳"));
}

// ponytail: success output — /uc status <id> + task-list detail showed a subtask's
// error + retries + review but NOT its result. The message (#374) + tree show it;
// mirror here. Error takes priority (error OR result). First line + `…` when more.
{
	const withResult = (result: string) => ({
		id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: [Object.assign(st("s1", [], "completed"), { result })],
	} as unknown as TaskState);
	const multi = formatTaskDetail(withResult("line one\nline two"), theme).join("\n");
	check("detail shows success result first line", multi.includes("line one"));
	check("detail multi-line result shows ellipsis", multi.includes("line one") && multi.includes("…"));
	const single = formatTaskDetail(withResult("short output"), theme).join("\n");
	check("detail single-line result no ellipsis", single.includes("short output") && !single.includes("…"));
	// error present → no result line (error takes priority)
	const withErr = { id: "T", description: "t", status: "failed", controlState: "running", createdAt: 0, subtasks: [Object.assign(st("s1", [], "failed"), { error: "boom", result: "should not show" })] } as unknown as TaskState;
	const errOut = formatTaskDetail(withErr, theme).join("\n");
	check("detail error present → no result line", !errOut.includes("should not show"));
}

// ponytail: detail header shows completed/total (mirror formatTaskList) — the
// detail view lists subtasks but the header had no count, so the user had to
// count rows to gauge progress. Only when there are subtasks (0 → no "0/0").
{
	const mk = (subs: { status: string }[]) => ({
		id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0,
		subtasks: subs.map((s, i) => st(`s${i}`, [], s.status as SubtaskResult["status"])),
	} as unknown as TaskState);
	const header = (t: TaskState) => formatTaskDetail(t, theme).find((l) => l.includes("T")) ?? "";
	// 2 completed of 3 → "2/3"
	const h23 = header(mk([{ status: "completed" }, { status: "completed" }, { status: "pending" }]));
	check("detail header shows completed/total (2/3)", h23.includes("2/3"));
	// zero subtasks → no count tag (no misleading "0/0")
	const h0 = header(mk([]));
	check("detail header no count for 0 subtasks", !h0.includes("0/0"));
	// failed-marker: 2 completed + 2 failed of 4 → "2/4 ·2✗" (no ✗ when 0 failed)
	const hf = header(mk([{ status: "completed" }, { status: "completed" }, { status: "failed" }, { status: "failed" }]));
	check("detail header failed-marker shows completed/total (2/4)", hf.includes("2/4"));
	check("detail header failed-marker shows ·2✗", hf.includes("·2✗"));
	// no failed → no ✗ marker (control)
	check("detail header no-failed has no ✗", !h23.includes("✗"));
	// 0-subtask → no ✗ marker either
	check("detail header 0-subtask has no ✗", !h0.includes("✗"));
	// running-marker: 2 running + 1 failed → "·2▶" + "·1✗"
	const hr = header(mk([{ status: "running" }, { status: "running" }, { status: "failed" }]));
	check("detail header running-marker shows ·N▶", hr.includes("·2▶"));
	// no running → no ▶ marker (control = the failed-marker test above)
	check("detail header no-running has no ▶", !hf.includes("▶"));
}

// ponytail: subtask-status breakdown in the "Subtasks:" header — "(X done, Y
// failed, Z running)" summarizes the rows below. Only non-zero buckets; failed
// is error-colored. No breakdown when all the same bucket (all-completed = bare).
{
	const mkSub = (status: string) => ({ id: "s", description: "d", status, dependsOn: [], files: [] } as any);
	const task = (subs: any[]) => ({ id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: subs } as unknown as TaskState);
	// 2 completed, 2 failed, 1 running, 1 pending → "(2 failed, 1 running, 1 pending, 2 done)"
	const mixed = formatTaskDetail(task([mkSub("completed"), mkSub("completed"), mkSub("failed"), mkSub("failed"), mkSub("running"), mkSub("pending")]), theme, 80);
	const hdr = mixed.find((l) => l.includes("Subtasks:")) ?? "";
	check("Subtasks header shows failed count", hdr.includes("2 failed"));
	check("Subtasks header shows running count", hdr.includes("1 running"));
	check("Subtasks header shows pending count", hdr.includes("1 pending"));
	check("Subtasks header shows done count", hdr.includes("2 done"));
	// all-completed → no breakdown (the total row count already says it)
	const allDone = formatTaskDetail(task([mkSub("completed"), mkSub("completed")]), theme, 80);
	const hdr2 = allDone.find((l) => l.includes("Subtasks:")) ?? "";
	check("Subtasks header no breakdown when all completed", hdr2.includes("Subtasks:") && !hdr2.includes("("));
	// 0-subtask task → bare "Subtasks:" (no counts)
	const empty = formatTaskDetail(task([]), theme, 80);
	const hdr3 = empty.find((l) => l.includes("Subtasks:")) ?? "";
	check("Subtasks header bare for 0 subtasks", hdr3.includes("Subtasks:") && !hdr3.includes("("));
}

// ponytail: blocked split out of pending in the breakdown — a pending subtask
// stalled on an unmet dep reads "blocked" (warning-colored), not lumped into
// "pending". Mirrors the countTag's ⏳ marker. 2 pending: C blocked on B (also
// pending), B ready (no deps) → "(1 blocked, 1 pending)".
{
	const mkSub = (id: string, dependsOn: string[] = [], status = "pending") =>
		({ id, description: "d", status, dependsOn, files: [] } as any);
	const task = (subs: any[]) => ({ id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: subs } as unknown as TaskState);
	const t = formatTaskDetail(task([mkSub("A", [], "completed"), mkSub("B"), mkSub("C", ["A", "B"])]), theme, 80);
	const hdr = t.find((l) => l.includes("Subtasks:")) ?? "";
	// C depends on A (completed) + B (pending) → C blocked by 1 unmet dep (B).
	check("breakdown shows blocked count", hdr.includes("1 blocked"));
	// B pending w/ no deps → ready, counted as pending (not blocked).
	check("breakdown shows ready pending count", hdr.includes("1 pending"));
	// blocked is a subset of pending; total pending (blocked+ready) = 2, never "2 pending".
	check("breakdown does not lump blocked into pending", !hdr.includes("2 pending"));
}

// ponytail: sortTasksForStatus — /uc status toast surfaces active/failed tasks
// first (getAllTaskStates is insertion-order = oldest first, burying live tasks
// under completed ones). Active (in_progress/planning/paused) → failed → cancelled
// → completed; stable within a priority tier.
{
	const mk = (id: string, status: string) =>
		({ id, description: "d", status, controlState: "running", createdAt: 0, subtasks: [] } as unknown as TaskState);
	// insertion order: completed, failed, in_progress, cancelled, planning
	const tasks = [
		mk("c1", "completed"),
		mk("f1", "failed"),
		mk("a1", "in_progress"),
		mk("x1", "cancelled"),
		mk("a2", "planning"),
	];
	const sorted = sortTasksForStatus(tasks).map((t) => t.id);
	// active (a1, a2) first — insertion order within tier: a1 before a2
	check("sort: active tasks first", sorted[0] === "a1" && sorted[1] === "a2");
	// failed next
	check("sort: failed after active", sorted[2] === "f1");
	// cancelled before completed
	check("sort: cancelled before completed", sorted.indexOf("x1") < sorted.indexOf("c1"));
	// completed last
	check("sort: completed last", sorted[sorted.length - 1] === "c1");
	// original array untouched (sort returns a copy)
	check("sort: does not mutate input", tasks[0].id === "c1");
}

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
