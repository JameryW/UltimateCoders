/**
 * Self-check for TaskList overlay navigation + detail mode.
 * Run: bun run src/ui/task-list-overlay.selfcheck.ts
 * Exits non-zero on failure. No test framework.
 *
 * ponytail: smallest check that fails if nav/detail logic breaks.
 */

import type { Theme, ThemeColor } from "@oh-my-pi/pi-coding-agent";
import type { TaskState } from "../orchestrator/orchestrator";
import { createTaskListOverlay } from "./task-list-overlay";

const theme: Theme = {
	fg: (_c: ThemeColor, text: string) => text,
	bold: (text: string) => text,
} as unknown as Theme;

function makeTask(id: string, status: TaskState["status"], subtaskCount = 2): TaskState {
	return {
		id, description: `task ${id}`, status, controlState: "running",
		createdAt: Date.now(), error: undefined,
		subtasks: Array.from({ length: subtaskCount }, (_, i) => ({
			id: `${id}-s${i}`, description: `sub ${i}`,
			status: i === 0 ? "completed" : "pending", dependsOn: [],
			result: undefined, error: undefined, review: undefined,
			retryCount: 0, dispatchMode: "prefer_remote",
		})),
	} as unknown as TaskState;
}

function makeComponent(tasks: TaskState[]) {
	const factory = createTaskListOverlay({
		tasks: () => tasks,
		getTask: (id) => tasks.find((t) => t.id === id),
		onClose: () => {},
	});
	let closed = false;
	const comp = factory(undefined, theme, undefined, () => { closed = true; }) as any;
	return { comp, closed: () => closed };
}

let failures = 0;
function check(name: string, cond: boolean) {
	console.log(`${cond ? "PASS" : "FAIL"}: ${name}`);
	if (!cond) failures++;
}

const UP = "\x1b[A", DOWN = "\x1b[B", PAGEUP = "\x1b[5~", PAGEDOWN = "\x1b[6~",
	HOME = "\x1b[H", END = "\x1b[F", ENTER = "\r", ESC = "\x1b";

// nav
{
	const { comp } = makeComponent([makeTask("t1","in_progress"), makeTask("t2","in_progress"), makeTask("t3","in_progress")]);
	comp.handleInput(DOWN); comp.handleInput(DOWN);
	check("down moves to bottom", comp.cursorIdx === 2);
	comp.handleInput(DOWN);
	check("down clamps at bottom", comp.cursorIdx === 2);
	comp.handleInput(UP);
	check("up moves back", comp.cursorIdx === 1);
}

// g/G + home/end
{
	const { comp } = makeComponent(Array.from({length:5},(_,i)=>makeTask(`t${i}`,"in_progress")));
	comp.handleInput("G"); check("G jumps to bottom", comp.cursorIdx === 4);
	comp.handleInput("g"); check("g jumps to top", comp.cursorIdx === 0);
	comp.handleInput(END); check("end jumps to bottom", comp.cursorIdx === 4);
	comp.handleInput(HOME); check("home jumps to top", comp.cursorIdx === 0);
}

// paging
{
	const { comp } = makeComponent(Array.from({length:50},(_,i)=>makeTask(`t${i}`,"in_progress")));
	comp.handleInput(PAGEDOWN); check("pageDown +20", comp.cursorIdx === 20);
	comp.handleInput(PAGEDOWN); check("pageDown +40", comp.cursorIdx === 40);
	comp.handleInput(PAGEUP); check("pageUp back 20", comp.cursorIdx === 20);
}

// detail mode
{
	const { comp } = makeComponent([makeTask("t1","in_progress")]);
	check("starts in list mode", comp.detailTaskId === null);
	comp.handleInput(ENTER);
	check("enter opens detail", comp.detailTaskId === "t1");
	const lines: string[] = comp.render(80);
	check("detail shows subtasks header", lines.some(l => l.includes("Subtasks")));
	comp.handleInput(ESC);
	check("esc returns to list", comp.detailTaskId === null);
}

// esc closes from list
{
	const { comp, closed } = makeComponent([makeTask("t1","in_progress")]);
	comp.handleInput(ESC);
	check("esc closes overlay from list", closed() === true);
}

// one line per task in list mode (age folded into main line, not a 2nd row)
// ponytail: invariant — maxVisible tasks == maxVisible rendered rows, else the
// window overflows the overlay height while the footer claims 1-N fit.
{
	const { comp } = makeComponent([makeTask("t1","in_progress"), makeTask("t2","in_progress")]);
	const lines = comp.render(80);
	// header(1) + hint(1) + blank(1) + 2 task rows = 5; age must NOT be its own row
	check("one row per task (no separate age line)", lines.length === 5);
	check("age present on task line", lines.some((l: string) => l.includes("ago")));
}

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
