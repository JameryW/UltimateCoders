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

// r on a non-failed subtask does NOT invoke onRetry
{
	let called = false;
	const { comp } = makeComponent(
		[makeSubtask("s1", { status: "completed" })],
		{ onRetry: () => { called = true; } },
	);
	comp.handleInput("r");
	check("r on completed subtask does not invoke onRetry", called === false);
}

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
