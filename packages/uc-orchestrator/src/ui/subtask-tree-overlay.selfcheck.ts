/**
 * Self-check for SubtaskTreeOverlay expanded detail (single-line preview).
 * Run: bun run src/ui/subtask-tree-overlay.selfcheck.ts
 *
 * ponytail: invariant — an expanded subtask adds at most ONE detail line
 * (was up to 9, overflowing the fixed overlay height with no scroll).
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

function makeComponent(subtasks: SubtaskResult[], opts?: { onRetry?: (taskId: string, subtaskId: string) => void }) {
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks,
	} as unknown as TaskState;
	const factory = createSubtaskTreeOverlay({
		tasks: () => [task],
		onRetry: opts?.onRetry ?? (() => {}),
		onClose: () => {},
	});
	let closed = false;
	// ponytail: pass a mock tui with requestRender — handleInput calls it at the end,
	// and undefined tui crashes (optional chaining guards the method, not the object).
	const mockTui = { requestRender: () => {} };
	const comp = factory(mockTui as any, theme, undefined, () => { closed = true; }) as any;
	return { comp, closed: () => closed };
}

// collapsed: one subtask → header + hint + blank + 1 row
{
	const { comp } = makeComponent([makeSubtask("s1")]);
	const lines = comp.render(80);
	// 3 chrome lines + 1 subtask row
	check("collapsed one subtask = 4 lines", lines.length === 4);
}

// expanded with all fields (error + result + review + retry + mode) → +1 line max
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
	// 4 (collapsed) + 1 detail line = 5, NOT 4+9
	check("expanded adds exactly 1 detail line", lines.length === 5);
	check("detail line present", lines.some((l: string) => l.includes("boom")));
}

// expanded with only result → 1 line, first result line shown
{
	const st = makeSubtask("s1", { status: "completed", result: "first line\nsecond" });
	const { comp } = makeComponent([st]);
	comp.expanded.add("s1");
	const lines = comp.render(80);
	check("result-only expanded = 5 lines", lines.length === 5);
	check("shows first result line", lines.some((l: string) => l.includes("first line")));
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

	// move cursor down to s2, retry again
	holder.args = null;
	comp.handleInput("\x1b[B"); // down
	comp.handleInput("R");
	const args2 = getArgs();
	check("R on second failed subtask invokes onRetry", args2 !== null);
	check("onRetry receives correct taskId (s2)", args2?.taskId === "T");
	check("onRetry receives cursor's subtaskId (s2)", args2?.subtaskId === "s2");
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

// ponytail: adaptive page size — small terminal (rows=10) yields < 20 visible items.
// maxVisible derives from tui.terminal.rows; with rows=10 → clamp(10-4,1,20)=6,
// so 50 subtasks render 3 chrome + 6 rows + 1 footer = 10 lines (not 24).
{
	const subs = Array.from({ length: 50 }, (_, i) => makeSubtask(`s${i}`));
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks: subs,
	} as unknown as TaskState;
	const factory = createSubtaskTreeOverlay({
		tasks: () => [task],
		onRetry: () => {},
		onClose: () => {},
	});
	const mockTui = { requestRender: () => {}, terminal: { rows: 10, columns: 80 } };
	const comp = factory(mockTui as any, theme, undefined, () => {}) as any;
	const lines = comp.render(80);
	check("small terminal shows < 20 item rows", lines.length < 24);
	check("small terminal page size = 6", lines.length === 10);
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

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
