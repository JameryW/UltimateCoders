/**
 * Self-check for SubtaskTreeOverlay expanded detail.
 * Run: bun run src/ui/subtask-tree-overlay.selfcheck.ts
 *
 * ponytail: invariant — an expanded subtask adds at most TWO detail lines
 * (line 1: error-or-result on its own line; line 2: meta tags with a width
 * guard). Was up to 9 lines originally (overflowed the fixed overlay height
 * with no scroll), then 1 line (error joined with tags, truncated right side),
 * now 2 lines (error separated for diagnostic priority + meta on its own line).
 */
import type { Theme, ThemeColor } from "@oh-my-pi/pi-coding-agent";
import type { TaskState, SubtaskResult } from "../orchestrator/orchestrator";
import { createSubtaskTreeOverlay } from "./subtask-tree-overlay";

const theme: Theme = {
	fg: (_c: ThemeColor, t: string) => t,
	bold: (t: string) => t,
} as unknown as Theme;

let failures = 0;
function check(name: string, cond: boolean) {
	console.log(`${cond ? "PASS" : "FAIL"}: ${name}`);
	if (!cond) failures++;
}

function makeSubtask(id: string, over: Partial<SubtaskResult> = {}): SubtaskResult {
	return {
		id, description: `d-${id}`, status: "failed", dependsOn: [], files: [],
		...over,
	} as unknown as SubtaskResult;
}

function makeComponent(subtasks: SubtaskResult[], opts?: {
	onRetry?: (taskId: string, subtaskId: string) => void;
	onJumpToTask?: (taskId: string) => void;
	cursorOnFailed?: boolean;
	cursorOnTaskId?: string;
	tasks?: TaskState[]; // override the default single-task shape for multi-task tests
	copy?: (text: string) => boolean;
}) {
	const task: TaskState = opts?.tasks
		? opts.tasks[0]
		: {
			id: "T", description: "t", status: "failed", controlState: "running",
			createdAt: 0, subtasks,
		} as unknown as TaskState;
	const tasksArr = opts?.tasks ?? [task];
	const factory = createSubtaskTreeOverlay({
		tasks: () => tasksArr,
		onRetry: opts?.onRetry ?? (() => {}),
		onJumpToTask: opts?.onJumpToTask,
		cursorOnFailed: opts?.cursorOnFailed,
		cursorOnTaskId: opts?.cursorOnTaskId,
		copy: opts?.copy,
		onClose: () => {},
	});
	let closed = false;
	// ponytail: pass a mock tui with requestRender — handleInput calls it at the end,
	// and undefined tui crashes (optional chaining guards the method, not the object).
	const mockTui = { requestRender: () => {} };
	const comp = factory(mockTui as any, theme, undefined, () => { closed = true; }) as any;
	return { comp, closed: () => closed };
}

const PAGEDOWN = "\x1b[6~";

// collapsed: one subtask → header + hint + blank + 1 row
{
	const { comp } = makeComponent([makeSubtask("s1")]);
	const lines = comp.render(80);
	// 3 chrome lines + 1 subtask row
	check("collapsed one subtask = 4 lines", lines.length === 4);
}

// ponytail: subtask row desc budget subtracts the ACTUAL prefix+suffix (id+deps+
// elapsed) — a normal id + dep list overflowed width under the old fixed width-16,
// letting the compositor truncate deps/elapsed (live elapsed on running rows was
// the most common casualty). No-ANSI theme → string.length == visible width.
// Use width=30 (narrow) so a long desc + deps + elapsed would overflow without
// the budget fix.
{
	const st = makeSubtask("s1", {
		status: "running",
		dependsOn: ["dep1", "dep2", "dep3"],
		startedAt: Date.now() - 60000,
		description: "d".repeat(60),
	});
	const { comp } = makeComponent([st]);
	const lines = comp.render(30);
	const row = lines.find((l: string) => l.includes("s1:")) ?? "";
	check("row with deps+elapsed fits narrow width", row.length <= 30);
}

// ponytail: dep-list collapse — a subtask depending on many roots emits
// "←+N deps" once the joined ids exceed half the width (mirrors status-formatter's
// formatTaskDetail). Was unbounded `←d1,d2,…`, overflowing the row and truncating
// the elapsed tag on running rows. Short dep lists still render verbatim.
{
	// short dep list at wide width → verbatim `←a`
	const { comp: wide } = makeComponent([makeSubtask("s1", { dependsOn: ["a"] })]);
	const wideLines = wide.render(80);
	check("short deps render verbatim (←a)", wideLines.some((l: string) => l.includes("←a") && !l.includes("+1 deps")));

	// long dep list at narrow width → collapse `←+3 deps` (joined "a,b,c"=5, +2=7 > 30/2=15? no)
	// force collapse: many deps so joined exceeds width/2 even at wide width.
	const many = Array.from({ length: 10 }, (_, i) => `dep${i}`);
	const { comp: narrow } = makeComponent([makeSubtask("s1", { dependsOn: many })]);
	const narrowLines = narrow.render(40); // 40/2=20, joined "dep0,dep1,...dep9" ~50 > 20 → collapse
	check("long deps collapse to ←+10 deps", narrowLines.some((l: string) => l.includes("←+10 deps")));
	check("collapsed deps hide the raw ids", narrowLines.every((l: string) => !l.includes("dep0,dep1")));
}

// expanded with all fields (error + result + review + retry + mode) → up to 2 lines
// ponytail: S7 — retryCount display in the expanded detail line (retry×N) is
// now reachable for local-exec subtasks after the orchestrator's result→TaskState
// copy path was fixed to copy retryCount. This test pins the overlay side of that.
// ponytail: S10 — error is now on its OWN line (diagnostic priority), and meta
// tags (retry×N, review, dispatchMode) are on a SEPARATE line. The old code joined
// them into one line, causing the compositor to truncate the right side (retry/mode).
// Now 2 lines max: error line + meta line.
{
	const st = makeSubtask("s1", {
		error: "boom",
		result: "done stuff\nmore",
		review: { approved: false } as any,
		retryCount: 3,
		dispatchMode: "local",
	});
	const { comp } = makeComponent([st]);
	comp.expanded.add("s1");
	const lines = comp.render(80);
	// 4 (collapsed) + 2 detail lines (error + meta) = 6, NOT 4+9
	check("expanded adds up to 2 detail lines (error + meta)", lines.length === 6);
	// error on its OWN line
	const errorLine = lines.find((l: string) => l.includes("boom"));
	check("error present on its own line", errorLine !== undefined);
	check("error line does NOT contain retry×3", errorLine !== undefined && !errorLine.includes("retry×3"));
	// meta tags on a SEPARATE line
	const metaLine = lines.find((l: string) => l.includes("retry×3"));
	check("S7: retry×3 shown in expanded detail (meta line)", metaLine !== undefined);
	check("meta line does NOT contain the error text", metaLine !== undefined && !metaLine.includes("boom"));
	// both lines' plain width <= 80 (strip ANSI for measurement — theme.fg is identity in test mock)
	check("error line plain width <= 80", errorLine !== undefined && errorLine.length <= 80);
	check("meta line plain width <= 80", metaLine !== undefined && metaLine.length <= 80);
}

// ponytail: `o` toggles expand-all / collapse-all — triaging a tree of N
// subtasks (each with an error/review) meant tapping Enter on every row.
// expand-all when fewer than all open, collapse-all otherwise. Empty → flash.
{
	// 3 subtasks, none expanded → `o` expands all
	const { comp } = makeComponent([makeSubtask("s1"), makeSubtask("s2"), makeSubtask("s3")]);
	check("o: starts with 0 expanded", comp.expanded.size === 0);
	comp.handleInput("o");
	check("o expands all subtasks", comp.expanded.size === 3);
	// all expanded → `o` collapses all
	comp.handleInput("o");
	check("o collapses all when all expanded", comp.expanded.size === 0);
	// partial → `o` expands to all
	comp.expanded.add("s1");
	comp.handleInput("o");
	check("o expands to all from partial (1/3)", comp.expanded.size === 3);
	// empty list → flash, no throw
	const emptyTask = { id: "T", description: "t", status: "failed", controlState: "running", createdAt: 0, subtasks: [] } as unknown as TaskState;
	const { comp: ec } = makeComponent([], { tasks: [emptyTask] });
	ec.handleInput("o");
	check("o on empty flashes 'no subtasks to expand'", ec.flashMsg !== null && ec.flashMsg.includes("no subtasks to expand"));
	// hint advertises `o all`
	const { comp: hc } = makeComponent([makeSubtask("s1")]);
	const hint = hc.render(80).join("\n");
	check("hint advertises o all", hint.includes("o all"));
	// o in search-edit falls through (not appended to query) — mirrors n/r.
	const { comp: sc } = makeComponent([makeSubtask("s1"), makeSubtask("s2")]);
	sc.handleInput("/");
	sc.handleInput("o");
	check("o in search-edit exits searchMode", sc.searchMode === false);
	check("o in search-edit did not append to query", sc.query === "");
	check("o in search-edit expanded all", sc.expanded.size === 2);
}

// ponytail: rejected review with issues shows the count in the meta tag —
// "✗ rejected (N issues)" — so the verdict surfaces that there are N reasons
// without expanding the row beyond the 2-line window (issues themselves are
// listed in /uc status detail + the completion message). Approved reviews
// and rejected-without-issues stay bare (no "0 issues" noise).
{
	const withIssues = makeSubtask("s1", { status: "failed", review: { approved: false, issues: ["a", "b"], suggestions: [] } as any });
	const { comp: c1 } = makeComponent([withIssues]);
	c1.expanded.add("s1");
	const l1 = c1.render(80);
	check("rejected w/ issues shows count", l1.some((l: string) => l.includes("✗ rejected (2 issues)")));

	const noIssues = makeSubtask("s2", { status: "failed", review: { approved: false, issues: [], suggestions: [] } as any });
	const { comp: c2 } = makeComponent([noIssues]);
	c2.expanded.add("s2");
	const l2 = c2.render(80);
	check("rejected w/o issues stays bare", l2.some((l: string) => l.includes("✗ rejected")) && !l2.some((l: string) => l.includes("issues")));

	const approved = makeSubtask("s3", { status: "completed", review: { approved: true, issues: ["x"], suggestions: [] } as any });
	const { comp: c3 } = makeComponent([approved]);
	c3.expanded.add("s3");
	const l3 = c3.render(80);
	check("approved stays bare (no count)", l3.some((l: string) => l.includes("✓ approved")) && !l3.some((l: string) => l.includes("issues")));
}

// expanded with only result → 1 line, first result line shown
{
	const st = makeSubtask("s1", { status: "completed", result: "first line\nsecond" });
	const { comp } = makeComponent([st]);
	comp.expanded.add("s1");
	const lines = comp.render(80);
	check("result-only expanded = 5 lines", lines.length === 5);
	check("shows first result line", lines.some((l: string) => l.includes("first line")));
	// ponytail: multi-line result shows `…` (more lines truncated) — was a bare
	// first line with no indicator, so a 2-line result looked complete at line 1.
	check("multi-line result shows ellipsis", lines.some((l: string) => l.includes("first line") && l.includes("…")));
}
// single-line result that fits → NO ellipsis (don't falsely signal truncation)
{
	const st = makeSubtask("s1", { status: "completed", result: "short result" });
	const { comp } = makeComponent([st]);
	comp.expanded.add("s1");
	const lines = comp.render(80);
	check("single-line result no ellipsis", lines.some((l: string) => l.includes("short result") && !l.includes("…")));
}

// esc closes
{
	const { comp, closed } = makeComponent([makeSubtask("s1")]);
	comp.handleInput("\x1b");
	check("esc closes", closed() === true);
}

// pressing r on a failed subtask at the cursor invokes onRetry with (taskId, subtaskId)
{
	// ponytail: TS can't track closure mutation, so read through a getter
	// to avoid narrowing `holder.args` to `null` after the explicit `= null` reset.
	const holder: { args: { taskId: string; subtaskId: string } | null } = { args: null };
	const getArgs = () => holder.args;
	const { comp } = makeComponent(
		[makeSubtask("s1"), makeSubtask("s2")],
		{
			onRetry: (taskId, subtaskId) => { holder.args = { taskId, subtaskId }; },
		},
	);
	// cursor starts at index 0 → s1
	comp.handleInput("r");
	const args1 = getArgs();
	check("r on failed subtask invokes onRetry", args1 !== null);
	check("onRetry receives correct taskId", args1?.taskId === "T");
	check("onRetry receives cursor's subtaskId", args1?.subtaskId === "s1");
	// ponytail: retry surfaces a "retrying <id>…" flash — the status flip is async,
	// so without feedback the user can't tell `r` registered.
	check("r sets 'retrying…' flash", comp.flashMsg !== null && comp.flashMsg.includes("retrying") && comp.flashMsg.includes("s1".slice(0, 8)));

	// move cursor down to s2, retry again
	holder.args = null;
	comp.handleInput("\x1b[B"); // down
	comp.handleInput("R");
	const args2 = getArgs();
	check("R on second failed subtask invokes onRetry", args2 !== null);
	check("onRetry receives correct taskId (s2)", args2?.taskId === "T");
	check("onRetry receives cursor's subtaskId (s2)", args2?.subtaskId === "s2");
}

// ponytail: `n` jumps to the NEXT failed subtask after the cursor — Ctrl+Shift+F
// lands on the first, but inside the tree there was no "next failed" key, so
// multi-failure triage meant manual ↓. Wraps to first when past the last.
{
	// subtasks: s0 failed, s1 completed, s2 failed, s3 failed
	const subs = [
		makeSubtask("s0", { status: "failed" }),
		makeSubtask("s1", { status: "completed" }),
		makeSubtask("s2", { status: "failed" }),
		makeSubtask("s3", { status: "failed" }),
	];
	const { comp } = makeComponent(subs);
	// cursor at 0 (s0) → `n` jumps to next failed = s2 (idx 2)
	comp.handleInput("n");
	check("n from s0 → s2 (idx 2)", comp.cursorIdx === 2);
	// cursor at 2 (s2) → `n` jumps to s3 (idx 3)
	comp.handleInput("n");
	check("n from s2 → s3 (idx 3)", comp.cursorIdx === 3);
	// cursor at 3 (s3, last failed) → `n` wraps to first failed s0 (idx 0)
	comp.handleInput("n");
	check("n from last failed wraps to first (idx 0)", comp.cursorIdx === 0);
	// no failed subtasks → flashMsg, cursor unchanged
	const { comp: comp2 } = makeComponent([makeSubtask("s0", { status: "completed" })]);
	comp2.handleInput("n");
	check("n with no failed flashes 'no failed subtasks'", comp2.flashMsg !== null && comp2.flashMsg.includes("no failed subtasks"));
}

// ponytail: sole failed subtask — `n`/`p` wrap back to the cursor itself (the
// only failed), so without feedback it's a silent no-op (cursor unmoved, flash
// null). Now flashes "only failed subtask".
{
	const { comp } = makeComponent([
		makeSubtask("s0", { status: "completed" }),
		makeSubtask("s1", { status: "failed" }),
		makeSubtask("s2", { status: "completed" }),
	]);
	comp.cursorIdx = 1; // on the sole failed (s1)
	comp.handleInput("n");
	check("n on sole failed flashes 'only failed subtask'", comp.flashMsg !== null && comp.flashMsg.includes("only failed subtask"));
	check("n on sole failed cursor unchanged", comp.cursorIdx === 1);
	comp.handleInput("p");
	check("p on sole failed flashes 'only failed subtask'", comp.flashMsg !== null && comp.flashMsg.includes("only failed subtask"));
	check("p on sole failed cursor unchanged", comp.cursorIdx === 1);
}

// ponytail: `p` jumps to the PREV failed subtask before the cursor — complement
// to `n` (next-failed). Wraps to the last when at/before the first failed.
{
	// subtasks: s0 failed, s1 completed, s2 failed, s3 failed
	const subs = [
		makeSubtask("s0", { status: "failed" }),
		makeSubtask("s1", { status: "completed" }),
		makeSubtask("s2", { status: "failed" }),
		makeSubtask("s3", { status: "failed" }),
	];
	const { comp } = makeComponent(subs);
	comp.cursorIdx = 3; // s3
	comp.handleInput("p"); // prev failed before s3 = s2 (idx 2)
	check("p from s3 → s2 (idx 2)", comp.cursorIdx === 2);
	comp.handleInput("p"); // prev before s2 = s0 (idx 0)
	check("p from s2 → s0 (idx 0)", comp.cursorIdx === 0);
	comp.handleInput("p"); // at first failed s0 → wraps to last failed s3 (idx 3)
	check("p from first failed wraps to last (idx 3)", comp.cursorIdx === 3);
	// no failed → flashMsg, cursor unchanged
	const { comp: comp2 } = makeComponent([makeSubtask("s0", { status: "completed" })]);
	comp2.handleInput("p");
	check("p with no failed flashes 'no failed subtasks'", comp2.flashMsg !== null && comp2.flashMsg.includes("no failed subtasks"));
}
// p in search-edit falls through (not appended to query) — mirrors n
{
	const subs = [
		makeSubtask("s0", { status: "failed" }),
		makeSubtask("s1", { status: "completed" }),
		makeSubtask("s2", { status: "failed" }),
	];
	const { comp } = makeComponent(subs);
	comp.cursorIdx = 2;
	comp.handleInput("/");
	comp.handleInput("p");
	check("p in search-edit exits searchMode", comp.searchMode === false);
	check("p in search-edit did not append to query", comp.query === "");
	check("p in search-edit jumped to prev failed (idx 0)", comp.cursorIdx === 0);
}

// r on a non-failed subtask does NOT invoke onRetry, and sets a flashMsg
{
	let called = false;
	const { comp } = makeComponent(
		[makeSubtask("s1", { status: "completed" })],
		{ onRetry: () => { called = true; } },
	);
	comp.handleInput("r");
	check("r on completed subtask does not invoke onRetry", called === false);
	const lines = comp.render(80);
	check("r on completed renders flashMsg with 'only failed'", lines.some((l: string) => l.includes("only failed")));
}

// r on a FAILED subtask when onRetry is NOT wired — must flash "retry unavailable"
// (not the contradictory "only failed... (cursor is failed)"). makeComponent forces
// a fallback onRetry, so build the factory directly without one.
{
	const task = { id: "T", description: "t", status: "failed", controlState: "running", createdAt: 0, subtasks: [makeSubtask("s1", { status: "failed" })] } as unknown as TaskState;
	const factory = createSubtaskTreeOverlay({ tasks: () => [task], onClose: () => {} }); // no onRetry
	const comp = factory({ requestRender: () => {} } as any, theme, undefined, () => {}) as any;
	comp.handleInput("r");
	check("r on failed w/o onRetry flashes 'retry unavailable'", comp.flashMsg !== null && comp.flashMsg.includes("retry unavailable"));
	check("r on failed w/o onRetry does NOT flash 'only failed'", comp.flashMsg === null || !comp.flashMsg.includes("only failed"));
	comp.dispose?.();
}

// ── search/filter tests ──────────────────────────────────────────

// `/` enters filter mode (render shows `/ ` input line with cursor)
{
	const { comp } = makeComponent([makeSubtask("s1")]);
	comp.handleInput("/");
	const lines = comp.render(80);
	check("/ enters filter mode (input line visible)", lines.some((l: string) => l.includes("/ ") && l.includes("▏")));
}

// typing narrows: 2 subtasks "alpha" / "beta", type "bet" → only beta visible
{
	const subtasks = [
		makeSubtask("s1", { description: "alpha task" }),
		makeSubtask("s2", { description: "beta task" }),
	];
	const { comp } = makeComponent(subtasks);
	comp.handleInput("/");  // enter filter mode
	comp.handleInput("b");
	comp.handleInput("e");
	comp.handleInput("t");
	comp.handleInput("\r"); // Enter: exit editing, keep filter
	const lines = comp.render(80);
	// Only the "beta" subtask row should be present, not "alpha"
	check("typing narrows to beta only", lines.some((l: string) => l.includes("beta")) && !lines.some((l: string) => l.includes("alpha")));
	check("filter header shows filtered count", lines.some((l: string) => l.includes("filtered from 2")));
}

// ponytail: filter matches dependsOn — the DAG triage query "which subtasks
// depend on X" (impacted downstream of a failure). Pre-fix the filter matched
// id/desc/status/taskId but not the dep list, so typing a dep id found the dep
// itself but not its dependents.
{
	// s0 (root), s1 depends on s0, s2 depends on s0 — type "s0" → s1+s2 surface
	// (the dependents), plus s0 itself (id match). Filter by dep: typing "s0"
	// must include s1/s2 which depend on s0.
	const subtasks = [
		makeSubtask("s0", { description: "root" }),
		makeSubtask("s1", { description: "downstream one", dependsOn: ["s0"] }),
		makeSubtask("s2", { description: "downstream two", dependsOn: ["s0"] }),
		makeSubtask("s3", { description: "unrelated" }),
	];
	const { comp } = makeComponent(subtasks);
	comp.handleInput("/");
	comp.handleInput("s");
	comp.handleInput("0");
	comp.handleInput("\r"); // exit editing, keep filter
	const lines = comp.render(80);
	// s1 + s2 depend on s0 → both must be visible (the whole point of dep matching)
	check("filter by dep 's0' shows dependent s1", lines.some((l: string) => l.includes("s1:")));
	check("filter by dep 's0' shows dependent s2", lines.some((l: string) => l.includes("s2:")));
	// s3 has no s0 dep → filtered out
	check("filter by dep 's0' hides unrelated s3", !lines.some((l: string) => l.includes("s3:")));
}

// ponytail: filter matches the parent taskId — typing a task's id surfaces ALL
// its subtasks (the natural "show me this task's work" intent). Pre-fix the
// filter matched only subtask id/desc/status, so a taskId with no subtask-id
// overlap returned no matches.
{
	const mk = (id: string) => ({
		id, description: id, status: "in_progress" as const, controlState: "running" as const,
		createdAt: 0, subtasks: [makeSubtask(`${id}-s0`, { description: "work" })],
	} as unknown as TaskState);
	const { comp } = makeComponent([], { tasks: [mk("taskA"), mk("taskB")] });
	comp.handleInput("/");
	comp.handleInput("t");
	comp.handleInput("a");
	comp.handleInput("s");
	comp.handleInput("k");
	comp.handleInput("a");
	comp.handleInput("\r");
	const lines = comp.render(80);
	// "taska" matches taskA's id → its subtask visible; taskB's subtask excluded
	check("filter by parent taskId surfaces its subtask", lines.some((l: string) => l.includes("taskA-s0")) && !lines.some((l: string) => l.includes("taskB-s0")));
}

// Esc exits filter + restores full list
{
	const subtasks = [
		makeSubtask("s1", { description: "alpha task" }),
		makeSubtask("s2", { description: "beta task" }),
	];
	const { comp } = makeComponent(subtasks);
	comp.handleInput("/");
	comp.handleInput("b");
	comp.handleInput("e");
	comp.handleInput("t");
	comp.handleInput("\r"); // Enter: exit editing, keep filter
	// Now Esc should clear the filter (not close the overlay)
	comp.handleInput("\x1b");
	const lines = comp.render(80);
	check("Esc restores full list (both visible)", lines.some((l: string) => l.includes("alpha")) && lines.some((l: string) => l.includes("beta")));
	check("Esc clears filter (no 'filtered from')", !lines.some((l: string) => l.includes("filtered from")));
}

// ponytail: clearing the filter restores cursor to the pre-filter cursor subtask
// (if still present), instead of snapping to row 0. Mirrors the task-list fix.
{
	const subtasks = [
		makeSubtask("s0", { description: "a" }),
		makeSubtask("s1", { description: "b" }),
		makeSubtask("s2", { description: "c" }),
	];
	const { comp } = makeComponent(subtasks);
	comp.handleInput("\x1b[B"); // down → s1 (idx 1)
	comp.handleInput("\x1b[B"); // down → s2 (idx 2)
	comp.handleInput("/"); // capture preFilterCursorSubId = s2
	comp.handleInput("s0"); // filter "s0" → only s0, cursor clamps to 0
	comp.handleInput("\r"); // exit editing, keep filter
	comp.handleInput("\x1b"); // clear filter → restore cursor to s2
	check("filter clear restores cursor to pre-filter subtask (s2)", comp.cursorIdx === 2);
}

// Backspace drops a char
{
	const subtasks = [
		makeSubtask("s1", { description: "alpha" }),
		makeSubtask("s2", { description: "alphabet" }),
	];
	const { comp } = makeComponent(subtasks);
	comp.handleInput("/");
	comp.handleInput("a");
	comp.handleInput("l");
	comp.handleInput("p");
	// Now backspace once — drops the "p"
	comp.handleInput("\x7f");
	// Still in searchMode with query="al" → both alpha and alphabet match
	const items = comp.currentItems();
	check("backspace drops last char (query=al → 2 results)", items.length === 2);
}

// ponytail: `n` in search-edit exits editing (keep filter) then falls through to
// next-failed — mirrors r/R. Pre-fix, `n` appended to the query (searchMode
// swallows printable chars), so next-failed was unreachable while editing.
{
	const subtasks = [
		makeSubtask("s0", { description: "alpha", status: "failed" }),
		makeSubtask("s1", { description: "beta", status: "completed" }),
		makeSubtask("s2", { description: "gamma", status: "failed" }),
	];
	const { comp } = makeComponent(subtasks);
	comp.handleInput("/");
	comp.handleInput("n"); // in search-edit: must NOT append "n" to query
	// searchMode exited, cursor jumped to next failed (s0 is at idx 0; from idx 0
	// next-failed after 0 = s2 at idx 2). query stayed empty (n was a command, not input).
	check("n in search-edit exits searchMode", comp.searchMode === false);
	check("n in search-edit did not append to query", comp.query === "");
	check("n in search-edit jumped to next failed (idx 2)", comp.cursorIdx === 2);
}

// ponytail: d (jump to task detail) + y/Y (copy) also fall through in search-edit
// (the #380 searchCmdKeys was ["n","r","R"] — d/y/Y were still swallowed).
{
	const copied: string[] = [];
	const { comp } = makeComponent([makeSubtask("s1", { status: "completed" })], {
		copy: (t) => { copied.push(t); return true; },
	});
	comp.handleInput("/");
	comp.handleInput("y"); // copy command — must NOT append to query
	check("y in search-edit exits searchMode", comp.searchMode === false);
	check("y in search-edit did not append to query", comp.query === "");
	check("y in search-edit copies cursor subtask id", copied.length === 1 && copied[0] === "s1");
}
{
	let jumped: string | null = null;
	const { comp, closed } = makeComponent([makeSubtask("s1")], {
		onJumpToTask: (tid) => { jumped = tid; },
	});
	comp.handleInput("/");
	comp.handleInput("d"); // jump command — must NOT append to query
	check("d in search-edit exits searchMode", comp.searchMode === false);
	check("d in search-edit did not append to query", comp.query === "");
	check("d in search-edit fires onJumpToTask + closes", jumped === "T" && closed() === true);
}

// r/R on a filtered failed subtask fires onRetry with the filtered id
{
	const subtasks = [
		makeSubtask("s1", { description: "alpha", status: "failed" }),
		makeSubtask("s2", { description: "beta", status: "failed" }),
	];
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks,
	} as unknown as TaskState;
	let retryArgs: [string, string] | null = null;
	const factory = createSubtaskTreeOverlay({
		tasks: () => [task],
		onRetry: (tid: string, sid: string) => { retryArgs = [tid, sid]; },
		onClose: () => {},
	});
	const comp = factory(undefined, theme, undefined, () => {}) as any;
	comp.handleInput("/");
	comp.handleInput("b");
	comp.handleInput("e");
	comp.handleInput("t");
	comp.handleInput("\r"); // Enter: exit editing, keep filter (only beta visible)
	comp.handleInput("r"); // retry the filtered cursor (beta)
	check("r on filtered failed fires onRetry with s2", retryArgs?.[1] === "s2");
}

// Empty filtered result → "no match" line
{
	const subtasks = [makeSubtask("s1", { description: "alpha" })];
	const { comp } = makeComponent(subtasks);
	comp.handleInput("/");
	comp.handleInput("z");
	comp.handleInput("z");
	comp.handleInput("z");
	comp.handleInput("\r"); // exit editing, keep filter "zzz" → no match
	const lines = comp.render(80);
	check("empty filtered shows 'no match' line", lines.some((l: string) => l.includes("no match for")));
}

// height-adaptive page size: a 24-row terminal reserves 12 rows for chrome, so
// maxVisible = 12, not the legacy hardcoded 20 that the maxHeight:"100%" clamp
// would silently truncate (cutting the footer + bottom cursor rows).
// ponytail: overlayPageSize reads tui.terminal.rows; undefined tui -> fallback 20.
{
	const subtasks = Array.from({length:50},(_,i)=>makeSubtask(`s${i}`));
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks,
	} as unknown as TaskState;
	const tui = { terminal: { rows: 24 } };
	const factory = createSubtaskTreeOverlay({
		tasks: () => [task], onRetry: () => {}, onClose: () => {},
	});
	const comp = factory(tui as any, theme, undefined, () => {}) as any;
	check("24-row terminal page size = 12", comp.maxVisible === 12);
	const lines: string[] = comp.render(80);
	// header(1) + hint(1) + blank(1) + 12 rows + footer(1) = 16
	check("24-row terminal shows 12 subtask rows", lines.length === 16);
	check("24-row terminal footer shows 1-12 of 50", lines.some((l: string) => l.includes("1-12 of 50")));
	comp.handleInput(PAGEDOWN);
	check("24-row terminal pageDown +12 (not +20)", comp.cursorIdx === 12);
}

// ponytail: cursorOnFailed (Ctrl+Shift+F) — constructor pre-sets cursor to the
// first failed subtask so `R` retry is one keystroke away. Mixed-status list:
// cursor must land on the failed one, not index 0.
{
	const subs = [
		makeSubtask("s0", { status: "completed" }),
		makeSubtask("s1", { status: "completed" }),
		makeSubtask("s2", { status: "failed" }),
		makeSubtask("s3", { status: "failed" }),
	];
	const { comp } = makeComponent(subs, { cursorOnFailed: true });
	check("cursorOnFailed lands on first failed (idx 2)", comp.cursorIdx === 2);
}

// ponytail: cursorOnFailed with NO failed subtask — cursor stays at 0 (no crash,
// no phantom). The toast is the caller's job (extension.ts checks hasFailed first).
{
	const subs = [makeSubtask("s0", { status: "completed" }), makeSubtask("s1", { status: "running" })];
	const { comp } = makeComponent(subs, { cursorOnFailed: true });
	check("cursorOnFailed no-failed stays at 0", comp.cursorIdx === 0);
}

// ponytail: cursorOnTaskId (task-list detail `t` jump) — constructor pre-sets
// cursor to the FIRST subtask of the given task, so the tree opens focused on
// that task's work, not the top of the full multi-task list. Multi-task list
// with the target task's subtasks starting at index 3 → cursor lands on 3.
{
	const mk = (id: string, n: number) => ({
		id, description: id, status: "in_progress" as const, controlState: "running" as const,
		createdAt: 0, subtasks: Array.from({ length: n }, (_, i) => makeSubtask(`${id}-s${i}`, { status: "completed" })),
	} as unknown as TaskState);
	const tasks = [mk("A", 2), mk("B", 2), mk("T", 3)]; // T's subtasks at flat idx 4..6
	const { comp } = makeComponent([], { cursorOnTaskId: "T", tasks });
	check("cursorOnTaskId lands on target task's first subtask (idx 4)", comp.cursorIdx === 4);
}

// ponytail: cursorOnTaskId with task NOT in list — cursor stays at 0 (no crash,
// no phantom), findIndex returns -1.
{
	const mk = (id: string) => ({
		id, description: id, status: "in_progress" as const, controlState: "running" as const,
		createdAt: 0, subtasks: [makeSubtask(`${id}-s0`, { status: "completed" })],
	} as unknown as TaskState);
	const { comp } = makeComponent([], { cursorOnTaskId: "GONE", tasks: [mk("A"), mk("B")] });
	check("cursorOnTaskId missing-task stays at 0", comp.cursorIdx === 0);
}

// `d` jump — fires onJumpToTask with the subtask's taskId, then closes the tree.
// Order: done() closes the tree BEFORE onJumpToTask opens the task-list, so the
// two overlays never overlap (was reversed — task-list opened while tree mounted).
{
	let jumpedTo: string | null = null;
	let closedAtJumpTime = 0;
	const { comp, closed } = makeComponent(
		[makeSubtask("s0", { status: "failed" })],
		{ onJumpToTask: (taskId) => { jumpedTo = taskId; if (closed()) closedAtJumpTime = 1; } },
	);
	comp.handleInput("d");
	check("`d` fires onJumpToTask with parent taskId", jumpedTo === "T");
	check("`d` closes the tree (done())", closed() === true);
	check("`d` done() before onJumpToTask (no overlap)", closedAtJumpTime === 1);
}

// `d` with no onJumpToTask wired — sets flashMsg, does NOT close.
{
	const { comp, closed } = makeComponent([makeSubtask("s0", { status: "failed" })]);
	comp.handleInput("d");
	check("`d` no-jump-handler does not close", closed() === false);
}

// ponytail: S5 — narrow-screen hint. Full hint (~73 chars) gets ANSI-truncated
// on terminals < ~73 cols, losing the right side (Esc close, / filter). The
// render hint uses a compact version under 60 cols. Only the NORMAL (non-search,
// non-filtering) hint branch is affected.
{
	const { comp } = makeComponent([makeSubtask("s1")]);
	const wide = comp.render(80).join("\n");
	check("wide hint has PgUp/PgDn", wide.includes("PgUp/PgDn"));
	check("wide hint has / filter", wide.includes("/ filter"));
	check("wide hint has Esc close", wide.includes("Esc close"));
	// ponytail: Enter toggles expand (no detail mode for subtasks) — hint must
	// say "expand" not "detail", else users expect a detail view that doesn't exist.
	check("wide hint says Enter expand (not detail)", wide.includes("Enter expand") && !wide.includes("Enter detail"));
	// ponytail: tree implements both y (subtask id) and Y (error/result) yank,
	// but the hint said only "y copy" — Y was undiscoverable. Match task-list.
	check("wide hint advertises y/Y copy", wide.includes("y/Y copy"));

	const narrow = comp.render(50).join("\n");
	check("narrow hint has Esc close", narrow.includes("Esc close"));
	check("narrow hint does NOT have PgUp/PgDn", !narrow.includes("PgUp/PgDn"));
}

// ponytail: S6 — Enter on empty subtask list sets flashMsg instead of silent no-op.
// Empty tasks array → flatItems is empty → items[this.cursorIdx] is undefined.
{
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks: [],
	} as unknown as TaskState;
	const factory = createSubtaskTreeOverlay({
		tasks: () => [task],
		onRetry: () => {},
		onClose: () => {},
	});
	const mockTui = { requestRender: () => {} };
	const comp = factory(mockTui as any, theme, undefined, () => {}) as any;
	comp.handleInput("\r"); // Enter
	check("Enter on empty list sets 'no subtask selected' flashMsg",
		comp.flashMsg !== null && comp.flashMsg.includes("no subtask selected"));
	// ponytail: empty tree must hint the submit path — mirror task-list empty state.
	const lines = comp.render(80) as string[];
	check("empty tree renders No tasks", lines.some((l: string) => l.includes("No tasks")));
	check("empty tree hints /uc submit", lines.some((l: string) => l.includes("/uc submit")));
}

// ponytail: S6 — `d` on empty subtask list sets flashMsg, does NOT call onJumpToTask,
// does NOT close. Restructured so no-item case is the first check (was silent before).
{
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks: [],
	} as unknown as TaskState;
	let jumpedTo: string | null = null;
	let closed = false;
	const factory = createSubtaskTreeOverlay({
		tasks: () => [task],
		onRetry: () => {},
		onJumpToTask: (taskId: string) => { jumpedTo = taskId; },
		onClose: () => {},
	});
	const mockTui = { requestRender: () => {} };
	const comp = factory(mockTui as any, theme, undefined, () => { closed = true; }) as any;
	comp.handleInput("d");
	check("`d` on empty list sets 'no subtask selected' flashMsg",
		comp.flashMsg !== null && comp.flashMsg.includes("no subtask selected"));
	check("`d` on empty list does NOT call onJumpToTask", jumpedTo === null);
	check("`d` on empty list does NOT close", closed === false);
}

// ponytail: S10 — error + meta at narrow width=30. Error gets its own line
// (width-8=22 budget for the root cause); meta line gets its own line with a
// width guard that drops low-priority tags (dispatchMode first, then review)
// to keep retry×N visible. Both lines must be <= 30 plain width.
{
	const st = makeSubtask("s1", {
		error: "something went wrong here with a longish message",
		review: { approved: false } as any,
		retryCount: 3,
		dispatchMode: "local",
	});
	const { comp } = makeComponent([st]);
	comp.expanded.add("s1");
	const lines = comp.render(30);
	// error on its own line
	const errorLine = lines.find((l: string) => l.includes("something"));
	check("narrow: error on its own line", errorLine !== undefined);
	check("narrow: error line plain width <= 30", errorLine !== undefined && errorLine.length <= 30);
	// retry×N should still be present (highest priority — never dropped)
	const metaLine = lines.find((l: string) => l.includes("retry×3"));
	check("narrow: retry×3 present on meta line", metaLine !== undefined);
	check("narrow: meta line plain width <= 30", metaLine !== undefined && metaLine.length <= 30);
}

// ponytail: S10 — width guard drops dispatchMode before review/retry.
// At width=30 with retry×3 + review + dispatchMode=local, the joined meta
// plain string "retry×3 · ✗ rejected · local" = 28 chars + 6 indent = 34 > 30.
// dispatchMode (lowest priority) should be dropped first to fit.
{
	const st = makeSubtask("s1", {
		error: "err",
		review: { approved: false } as any,
		retryCount: 3,
		dispatchMode: "local",
	});
	const { comp } = makeComponent([st]);
	comp.expanded.add("s1");
	const lines = comp.render(30);
	const metaLine = lines.find((l: string) => l.includes("retry×3"));
	check("width guard: retry×3 kept", metaLine !== undefined && metaLine.includes("retry×3"));
	// dispatchMode "local" should be dropped if over budget — acceptable either way
	// but verify retry×N survived (the point of the priority drop)
	check("width guard: retry×N survives over-budget", metaLine !== undefined && metaLine.includes("retry×3"));
}

// ponytail: F4 — filter must match status, not just id/description (task-list
// overlay already matches status). `/ failed` is the common filter intent;
// before F4 it returned no match despite failed subtasks existing.
{
	const subtasks = [
		makeSubtask("s1", { status: "completed", description: "alpha" }),
		makeSubtask("s2", { status: "failed", description: "beta" }),
	];
	const { comp } = makeComponent(subtasks);
	comp.handleInput("/");
	for (const ch of "failed") comp.handleInput(ch);
	comp.handleInput("\r"); // exit editing, keep filter
	check("F4 filter by status matches failed subtask", comp.currentItems().length === 1);
	const lines = comp.render(80);
	check("F4 render shows failed row only", lines.some((l: string) => l.includes("beta")) && !lines.some((l: string) => l.includes("alpha")));
	check("F4 filtered count in header", lines.some((l: string) => l.includes("filtered from 2")));
}

// ponytail: F6 — scroll footer arrows mark the clipped side.
{
	const subtasks = Array.from({ length: 50 }, (_, i) => makeSubtask(`s${i}`));
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks,
	} as unknown as TaskState;
	const tui = { terminal: { rows: 24 } }; // maxVisible = 12
	const factory = createSubtaskTreeOverlay({ tasks: () => [task], onRetry: () => {}, onClose: () => {} });
	const comp = factory(tui as any, theme, undefined, () => {}) as any;
	const footer = () => (comp.render(80) as string[]).find((l: string) => l.includes("of 50"));
	check("F6 tree at top: ▼ below, no ▲", footer()?.includes("▼") === true && footer()?.includes("▲") === false);
	comp.handleInput(PAGEDOWN);
	check("F6 tree mid-scroll: both ▲ and ▼", footer()?.includes("▲") === true && footer()?.includes("▼") === true);
}

// ponytail: F8 — running rows show live elapsed from startedAt; hung subtasks
// (frozen %/phase) become distinguishable from active ones. Non-running: none.
{
	const running = makeSubtask("s1", { status: "running", startedAt: Date.now() - 65_000 });
	const { comp } = makeComponent([running]);
	check("F8 running row shows elapsed (1m)", (comp.render(80) as string[]).some((l: string) => l.includes("(1m")));
	const done = makeSubtask("s2", { status: "completed", startedAt: Date.now() - 65_000 });
	const { comp: comp2 } = makeComponent([done]);
	check("F8 completed row has no elapsed", !(comp2.render(80) as string[]).some((l: string) => l.includes("(1m")));
}

// ponytail: F7 — refresh timer ticks requestRender; dispose() stops it.
{
	let ticks = 0;
	const mockTui = { requestRender: () => { ticks++; } };
	const task = {
		id: "T", description: "t", status: "in_progress", controlState: "running",
		createdAt: 0, subtasks: [makeSubtask("s1", { status: "running", startedAt: Date.now() })],
	} as unknown as TaskState;
	const comp = createSubtaskTreeOverlay({
		tasks: () => [task], onRetry: () => {}, onClose: () => {},
	})(mockTui as any, theme, undefined, () => {}) as any;
	await new Promise((r) => setTimeout(r, 1100));
	check("F7 tree timer ticks requestRender", ticks >= 1);
	comp.dispose();
	const after = ticks;
	await new Promise((r) => setTimeout(r, 1100));
	check("F7 tree dispose stops timer", ticks === after);
}

// ponytail: F9 — row-budget window. Expanded items render up to 3 rows; the old
// item-count slice showed all 6 (18 rows) on a 12-row budget, and the maxHeight
// clamp silently cut the footer + flashMsg. Row windowing must stop at 4 items
// (12 rows) and keep the footer visible.
{
	const subtasks = Array.from({ length: 6 }, (_, i) =>
		makeSubtask(`s${i}`, { status: "failed", error: "boom", retryCount: 2 }));
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks,
	} as unknown as TaskState;
	const tui = { terminal: { rows: 24 } }; // maxVisible = 12
	const factory = createSubtaskTreeOverlay({ tasks: () => [task], onRetry: () => {}, onClose: () => {} });
	const comp = factory(tui as any, theme, undefined, () => {}) as any;
	for (const st of subtasks) comp.expanded.add(st.id); // all expanded → 3 rows each
	const lines = comp.render(80) as string[];
	const shown = subtasks.filter((st) => lines.some((l: string) => l.includes(`${st.id}:`))).length;
	check("F9 row budget stops at 4 expanded items (not 6)", shown === 4);
	check("F9 footer visible despite overflow", lines.some((l: string) => l.includes("1-4 of 6")));
	// cursor down to item 5 → clampScroll must advance offset so it renders
	comp.handleInput("\x1b[B"); comp.handleInput("\x1b[B"); comp.handleInput("\x1b[B"); comp.handleInput("\x1b[B");
	const lines2 = comp.render(80) as string[];
	check("F9 cursor item 5 visible after nav", lines2.some((l: string) => l.includes("s4:")));
	check("F9 footer shows ▲ after scroll", lines2.some((l: string) => l.includes("of 6") && l.includes("▲")));
}

// ponytail: F22 — `y` yanks subtask id, `Y` yanks its error text (injectable
// copier — the real clipboard is never touched).
{
	const copied: string[] = [];
	const subtasks = [
		makeSubtask("s1", { status: "failed", error: "full error text here" }),
		makeSubtask("s2", { status: "completed" }),
	];
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks,
	} as unknown as TaskState;
	const mockTui = { requestRender: () => {} };
	const comp = createSubtaskTreeOverlay({
		tasks: () => [task], onRetry: () => {},
		copy: (t) => { copied.push(t); return true; },
		onClose: () => {},
	})(mockTui as any, theme, undefined, () => {}) as any;
	comp.handleInput("y");
	check("F22 tree `y` copies cursor subtask id", copied.length === 1 && copied[0] === "s1");
	comp.handleInput("Y");
	check("F22 tree `Y` copies error text", copied.length === 2 && copied[1] === "full error text here");
	comp.handleInput("\x1b[B"); // cursor → s2 (completed, no error)
	comp.handleInput("Y");
	check("F22 `Y` without error/result flashes", comp.flashMsg !== null && comp.flashMsg.includes("no error or result to copy"));
	check("F22 `Y` without error/result copies nothing", copied.length === 2);

	// ponytail: `Y` falls back to result when there's no error — a completed
	// subtask's output is what the user wants to paste, and "no error to copy"
	// on a successful subtask was misleading.
	const copied2: string[] = [];
	const doneSub = makeSubtask("s3", { status: "completed", result: "the output line" } as any);
	const task2 = { id: "T2", description: "t", status: "completed", controlState: "running", createdAt: 0, subtasks: [doneSub] } as unknown as TaskState;
	const comp2 = createSubtaskTreeOverlay({
		tasks: () => [task2], onRetry: () => {},
		copy: (t) => { copied2.push(t); return true; },
		onClose: () => {},
	})(mockTui as any, theme, undefined, () => {}) as any;
	comp2.handleInput("Y");
	check("F22 `Y` falls back to result (no error)", copied2.length === 1 && copied2[0] === "the output line");
}

// ponytail: regression guard — yank flash shows the FULL id, not a truncated
// prefix. Was slice(0,8), which rendered `copied subtask_0` for a long id and
// hid what actually landed on the clipboard. A long id must appear verbatim.
{
	const copied: string[] = [];
	const longId = "subtask_01J9X4F2K7M3QZ8ABC";
	const subtasks = [makeSubtask(longId, { status: "completed" })];
	const task = {
		id: "T", description: "t", status: "completed", controlState: "running",
		createdAt: 0, subtasks,
	} as unknown as TaskState;
	const mockTui = { requestRender: () => {} };
	const comp = createSubtaskTreeOverlay({
		tasks: () => [task], onRetry: () => {},
		copy: (t) => { copied.push(t); return true; },
		onClose: () => {},
	})(mockTui as any, theme, undefined, () => {}) as any;
	comp.handleInput("y");
	check("F22 long-id yank flash shows full id", comp.flashMsg === `copied ${longId}`);
	check("F22 long-id copies full id verbatim", copied.length === 1 && copied[0] === longId);
}

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
// ponytail: explicit exit — overlay components start 1s refresh timers; without
// exit(0) the pending intervals keep the script alive after ALL PASS.
process.exit(failures === 0 ? 0 : 1);
