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

function makeComponent(tasks: TaskState[], opts?: {
	onAction?: (taskId: string, action: "cancel" | "pause" | "resume") => boolean | Promise<boolean>;
	initialDetailTaskId?: string;
	onJumpToTree?: (taskId: string) => void;
	copy?: (text: string) => boolean;
}) {
	const factory = createTaskListOverlay({
		tasks: () => tasks,
		getTask: (id) => tasks.find((t) => t.id === id),
		onAction: opts?.onAction,
		initialDetailTaskId: opts?.initialDetailTaskId,
		onJumpToTree: opts?.onJumpToTree,
		copy: opts?.copy,
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

// ponytail: `n` jumps to the NEXT failed TASK — the subtask-tree has n for
// subtask-level; the task-list had no task-level jump, so finding the next
// failed task among many meant ↓ or /failed filter. Wraps to first past last.
{
	// t0 failed, t1 in_progress, t2 failed, t3 failed
	const tasks = [
		makeTask("t0", "failed"),
		makeTask("t1", "in_progress"),
		makeTask("t2", "failed"),
		makeTask("t3", "failed"),
	];
	const { comp } = makeComponent(tasks);
	comp.handleInput("n"); // cursor 0 (t0) → next failed t2 (idx 2)
	check("n from t0 → t2 (idx 2)", comp.cursorIdx === 2);
	comp.handleInput("n"); // t2 → t3 (idx 3)
	check("n from t2 → t3 (idx 3)", comp.cursorIdx === 3);
	comp.handleInput("n"); // t3 (last failed) → wraps to t0 (idx 0)
	check("n from last failed wraps to first (idx 0)", comp.cursorIdx === 0);
	// no failed → flashMsg, cursor unchanged
	const { comp: comp2 } = makeComponent([makeTask("t0", "in_progress")]);
	comp2.handleInput("n");
	check("n with no failed flashes 'no failed tasks'", comp2.flashMsg !== null && comp2.flashMsg.includes("no failed tasks"));
}
// ponytail: `N` — prev-failed (complement of `n`). `p` is pause, so prev-failed
// has no lowercase home; `N` (uppercase, unused) mirrors vim N/n. Wraps to the
// last failed when the cursor is at/before the first.
{
	const tasks = [
		makeTask("t0", "failed"),
		makeTask("t1", "in_progress"),
		makeTask("t2", "failed"),
		makeTask("t3", "failed"),
	];
	const { comp } = makeComponent(tasks);
	comp.cursorIdx = 2; // on t2 → prev failed = t0 (idx 0)
	comp.handleInput("N");
	check("N from t2 → t0 (idx 0)", comp.cursorIdx === 0);
	comp.handleInput("N"); // t0 (first failed) → wraps to t3 (idx 3, last failed)
	check("N from first failed wraps to last (idx 3)", comp.cursorIdx === 3);
	comp.handleInput("N"); // t3 → t2 (idx 2)
	check("N from t3 → t2 (idx 2)", comp.cursorIdx === 2);
	// no failed → flashMsg, cursor unchanged
	const { comp: comp2 } = makeComponent([makeTask("t0", "in_progress")]);
	comp2.handleInput("N");
	check("N with no failed flashes 'no failed tasks'", comp2.flashMsg !== null && comp2.flashMsg.includes("no failed tasks"));
}
// ponytail: sole failed task — `N` wraps back to the cursor itself → "only failed task".
{
	const { comp } = makeComponent([
		makeTask("t0", "completed"),
		makeTask("t1", "failed"),
		makeTask("t2", "in_progress"),
	]);
	comp.cursorIdx = 1; // on the sole failed (t1)
	comp.handleInput("N");
	check("N on sole failed flashes 'only failed task'", comp.flashMsg !== null && comp.flashMsg.includes("only failed task"));
	check("N on sole failed cursor unchanged", comp.cursorIdx === 1);
}
// N in search-edit falls through (not appended to query) — mirrors `n`.
{
	const tasks = [makeTask("t0", "failed"), makeTask("t1", "in_progress"), makeTask("t2", "failed")];
	const { comp } = makeComponent(tasks);
	comp.cursorIdx = 0;
	comp.handleInput("/");
	comp.handleInput("N");
	check("N in search-edit exits searchMode", comp.searchMode === false);
	check("N in search-edit did not append to query", comp.query === "");
	check("N in search-edit jumped to prev failed (idx 2)", comp.cursorIdx === 2);
}
// ponytail: sole failed task — `n` wraps back to the cursor itself, so without
// feedback it's a silent no-op. Now flashes "only failed task".
{
	const { comp } = makeComponent([
		makeTask("t0", "completed"),
		makeTask("t1", "failed"),
		makeTask("t2", "in_progress"),
	]);
	comp.cursorIdx = 1; // on the sole failed (t1)
	comp.handleInput("n");
	check("n on sole failed flashes 'only failed task'", comp.flashMsg !== null && comp.flashMsg.includes("only failed task"));
	check("n on sole failed cursor unchanged", comp.cursorIdx === 1);
}
// n in search-edit falls through (not appended to query)
{
	const tasks = [makeTask("t0", "failed"), makeTask("t1", "in_progress"), makeTask("t2", "failed")];
	const { comp } = makeComponent(tasks);
	comp.cursorIdx = 2;
	comp.handleInput("/");
	comp.handleInput("n");
	check("n in search-edit exits searchMode", comp.searchMode === false);
	check("n in search-edit did not append to query", comp.query === "");
	check("n in search-edit jumped to next failed (idx 0)", comp.cursorIdx === 0);
}

// paging
{
	const { comp } = makeComponent(Array.from({length:50},(_,i)=>makeTask(`t${i}`,"in_progress")));
	comp.handleInput(PAGEDOWN); check("pageDown +20", comp.cursorIdx === 20);
	comp.handleInput(PAGEDOWN); check("pageDown +40", comp.cursorIdx === 40);
	comp.handleInput(PAGEUP); check("pageUp back 20", comp.cursorIdx === 20);
}

// height-adaptive page size: a 24-row terminal reserves 12 rows for chrome, so
// maxVisible = 12, not the legacy hardcoded 20 that the maxHeight:"100%" clamp
// would silently truncate (cutting the footer + bottom cursor rows).
// ponytail: overlayPageSize reads tui.terminal.rows; undefined tui -> fallback 20.
{
	const tui = { terminal: { rows: 24 } };
	const tasks = Array.from({length:50},(_,i)=>makeTask(`t${i}`,"in_progress"));
	const factory = createTaskListOverlay({
		tasks: () => tasks, getTask: () => undefined, onClose: () => {},
	});
	const comp = factory(tui as any, theme, undefined, () => {}) as any;
	check("24-row terminal page size = 12", comp.maxVisible === 12);
	const lines: string[] = comp.render(80);
	// header(1) + hint(1) + blank(1) + 12 rows + footer(1) = 16
	check("24-row terminal renders 12 item rows + chrome", lines.length === 16);
	check("24-row terminal footer shows 1-12 of 50", lines.some((l: string) => l.includes("1-12 of 50")));
	comp.handleInput(PAGEDOWN);
	check("24-row terminal pageDown +12 (not +20)", comp.cursorIdx === 12);
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
	// ponytail: `q` mirrors list-mode back key in detail (Esc/q consistency)
	comp.handleInput(ENTER); // reopen detail
	check("re-enter detail", comp.detailTaskId === "t1");
	comp.handleInput("q");
	check("`q` returns to list from detail", comp.detailTaskId === null);
}

// ponytail: `n` in detail — jump to next failed task, exit detail back to list.
// cursor lands on the failed task in list mode (detailTaskId cleared). Wraps
// around the set; no failed → flashMsg, stays in detail.
{
	const { comp } = makeComponent([
		makeTask("t0", "completed"),
		makeTask("t1", "in_progress"),
		makeTask("t2", "failed"),
		makeTask("t3", "failed"),
	]);
	comp.handleInput(ENTER); // open detail on t0 (cursor 0)
	check("n-detail: detail open", comp.detailTaskId === "t0");
	comp.handleInput("n"); // t0 → next failed = t2
	check("n-detail: exits detail to list", comp.detailTaskId === null);
	check("n-detail: cursor on t2", comp.cursorIdx === 2);
	comp.handleInput(ENTER); // open detail on t2
	check("n-detail: reopen detail on t2", comp.detailTaskId === "t2");
	comp.handleInput("n"); // t2 → t3
	check("n-detail: cursor on t3 after second n", comp.cursorIdx === 3);
	check("n-detail: exits detail again", comp.detailTaskId === null);
	// no failed → flashMsg, stay in detail
	const { comp: comp2 } = makeComponent([makeTask("s1", "completed")]);
	comp2.handleInput(ENTER);
	comp2.handleInput("n");
	check("n-detail: no failed flashes", comp2.flashMsg !== null && comp2.flashMsg.includes("no failed tasks"));
	check("n-detail: no failed stays in detail", comp2.detailTaskId === "s1");
}

// ponytail: `n` in detail on the sole failed task flashes "only failed task"
// and stays in detail — was silently exiting detail (detailTaskId=null) with no
// feedback, because `n` wrapped back to the cursor itself. Mirrors list-mode fix.
{
	const { comp } = makeComponent([
		makeTask("t0", "completed"),
		makeTask("t1", "failed"),
		makeTask("t2", "in_progress"),
	]);
	comp.cursorIdx = 1; // cursor on t1 (sole failed)
	comp.handleInput(ENTER); // open detail on t1
	comp.handleInput("n"); // sole failed → flash, stay in detail
	check("n-detail sole failed flashes 'only failed task'", comp.flashMsg !== null && comp.flashMsg.includes("only failed task"));
	check("n-detail sole failed stays in detail", comp.detailTaskId === "t1");
}

// ponytail: `t` in detail opens the subtask tree on this task (reverse of the
// tree's `d` → detail jump). Verify it closes the list + fires onJumpToTree
// with the detail task id; without an opener it flashes "jump unavailable".
{
	const jumped: string[] = [];
	const { comp, closed } = makeComponent([makeTask("t1", "in_progress")], {
		onJumpToTree: (id) => { jumped.push(id); },
	});
	comp.handleInput(ENTER); // open detail on t1
	check("t-jump: detail open", comp.detailTaskId === "t1");
	comp.handleInput("t");
	check("t-jump: fires onJumpToTree with detail id", jumped.length === 1 && jumped[0] === "t1");
	check("t-jump: closes the list overlay", closed() === true);

	// no opener wired → flashMsg instead of silent no-op
	const { comp: comp2 } = makeComponent([makeTask("t2", "in_progress")]);
	comp2.handleInput(ENTER);
	comp2.handleInput("t");
	check("t-jump: no opener flashes 'jump unavailable'", comp2.flashMsg !== null && comp2.flashMsg.includes("jump unavailable"));
}

// ponytail: `t` in LIST mode — jump from cursor task straight to its subtask
// tree, mirroring detail-mode `t` (skips the Enter→detail step). Verify it
// closes the list + fires onJumpToTree with the cursor task id; no opener /
// no cursor task → flashMsg.
{
	const jumped: string[] = [];
	const { comp, closed } = makeComponent(
		[makeTask("t1", "in_progress"), makeTask("t2", "in_progress")],
		{ onJumpToTree: (id) => { jumped.push(id); } },
	);
	comp.cursorIdx = 1; // cursor on t2
	comp.handleInput("t");
	check("t-list: fires onJumpToTree with cursor id", jumped.length === 1 && jumped[0] === "t2");
	check("t-list: closes the list overlay", closed() === true);

	// no opener wired → flashMsg instead of silent no-op
	const { comp: comp2 } = makeComponent([makeTask("t1", "in_progress")]);
	comp2.handleInput("t");
	check("t-list: no opener flashes 'jump unavailable'", comp2.flashMsg !== null && comp2.flashMsg.includes("jump unavailable"));
}

// ponytail: `t` in list on a 0-subtask task flashes "no subtasks" — jumping
// would open an empty tree, wasting a round-trip. Mirrors the detail-mode guard.
{
	const jumped: string[] = [];
	const { comp, closed } = makeComponent([makeTask("t1", "in_progress", 0)], {
		onJumpToTree: (id) => { jumped.push(id); },
	});
	comp.handleInput("t"); // 0 subtasks → flash, no jump
	check("t-list 0-subtask flashes 'no subtasks'", comp.flashMsg !== null && comp.flashMsg.includes("no subtasks"));
	check("t-list 0-subtask does not fire onJumpToTree", jumped.length === 0);
	check("t-list 0-subtask does not close", closed() === false);
}

// ponytail: `t` is a letter users type into filters ("beta"), so in search-edit
// it appends to the query (NOT a fall-through command key like c/p/r/y/n).
// Exit editing first (Esc/Enter/nav) to use `t` as a subtask-tree jump.
{
	const { comp } = makeComponent([makeTask("t1", "in_progress")], {
		onJumpToTree: () => {},
	});
	comp.handleInput("/");
	comp.handleInput("t");
	check("t-list: search-edit appends 't' to query", comp.query === "t");
	check("t-list: search-edit stays in searchMode", comp.searchMode === true);
}

// ponytail: `t` on a 0-subtask task flashes "no subtasks" — jumping would open
// an empty tree, wasting a round-trip. The detail view already shows the empty
// subtask list, so flash instead.
{
	const jumped: string[] = [];
	const { comp, closed } = makeComponent([makeTask("t1", "in_progress", 0)], {
		onJumpToTree: (id) => { jumped.push(id); },
	});
	comp.handleInput("t"); // 0 subtasks → flash, no jump
	check("t-list 0-subtask flashes 'no subtasks'", comp.flashMsg !== null && comp.flashMsg.includes("no subtasks"));
	check("t-list 0-subtask does not fire onJumpToTree", jumped.length === 0);
	check("t-list 0-subtask does not close", closed() === false);
}

// ponytail: `t` is a letter users type into filters ("beta"), so in search-edit
// it appends to the query (NOT a fall-through command key like c/p/r/y/n).
// Exit editing first (Esc/Enter/nav) to use `t` as a subtask-tree jump.
{
	const { comp } = makeComponent([makeTask("t1", "in_progress")], {
		onJumpToTree: () => {},
	});
	comp.handleInput("/");
	comp.handleInput("t");
	check("t-list: search-edit appends 't' to query", comp.query === "t");
	check("t-list: search-edit stays in searchMode", comp.searchMode === true);
}

// detail-mode `t` 0-subtask guard
{
	const jumped2: string[] = [];
	const { comp, closed } = makeComponent([makeTask("t1", "in_progress", 0)], {
		onJumpToTree: (id) => { jumped2.push(id); },
	});
	comp.handleInput(ENTER); // open detail on t1
	comp.handleInput("t"); // 0 subtasks → flash, no jump
	check("t-jump 0-subtask flashes 'no subtasks'", comp.flashMsg !== null && comp.flashMsg.includes("no subtasks"));
	check("t-jump 0-subtask does not fire onJumpToTree", jumped2.length === 0);
	check("t-jump 0-subtask stays in detail (no close)", closed() === false && comp.detailTaskId === "t1");
}

// detail mode must not raw-slice ANSI-themed lines: String.slice on a
// theme-colored detailLine splits escape sequences and drops the closing reset,
// bleeding color / garbling the display. Rely on the compositor's ANSI-aware
// truncation instead.
// ponytail: renderDetail used l.slice(0, width-2) on ANSI-laden detailLines.
{
	const ansiTheme: Theme = {
		fg: (_c: ThemeColor, t: string) => `\x1b[36m${t}\x1b[0m`,
		bold: (t: string) => `\x1b[1m${t}\x1b[0m`,
	} as unknown as Theme;
	const task = makeTask("t1", "failed");
	task.description = "x".repeat(80); // long -> Description line exceeds narrow width
	const comp = createTaskListOverlay({
		tasks: () => [task], getTask: () => task, onClose: () => {},
	})(undefined, ansiTheme, undefined, () => {}) as any;
	comp.handleInput(ENTER);
	check("ansi: detail open", comp.detailTaskId === "t1");
	const lines: string[] = comp.render(30); // narrow
	// The long themed "Description:" line must keep its closing \x1b[0m reset;
	// raw slice(0, width-2) would have cut it off mid-line (no reset).
	check(
		"ansi: detail line reset preserved (no raw slice)",
		lines.some((l: string) => l.includes("Description:") && l.endsWith("\x1b[0m")),
	);
}

// ponytail: detail refreshes from the live task on each render — the snapshot
// taken in openDetail goes stale when the task's status changes (external event
// or the detail's own c/p/r), so the detail view must reflect the new status
// without reopening. getTask returns the same mutable object; flip its status.
{
	const task = makeTask("t1", "in_progress");
	const comp = createTaskListOverlay({
		tasks: () => [task], getTask: () => task, onClose: () => {},
	})(undefined, theme, undefined, () => {}) as any;
	comp.handleInput(ENTER);
	let lines: string[] = comp.render(80);
	check("detail shows in_progress status", lines.some((l: string) => l.includes("in_progress")));
	// mutate the live task — render must pick up the new status
	(task as any).status = "failed";
	lines = comp.render(80);
	check("detail refreshes to failed status", lines.some((l: string) => l.includes("failed")));
	check("detail no longer shows in_progress", !lines.some((l: string) => l.includes("in_progress")));
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

// ponytail: list row desc budget subtracts the ACTUAL countTag length — a task
// with a long countTag ("10/10") overflowed width under the old fixed width-34
// budget (assumed "2/3"=4 chars), letting the compositor truncate the desc.
// No-ANSI theme → string.length == visible width.
{
	const task = makeTask("t1", "in_progress", 10);
	task.description = "d".repeat(60);
	const { comp } = makeComponent([task]);
	const lines = comp.render(40);
	const row = lines.find((l: string) => l.includes("t1")) ?? "";
	check("row with long countTag fits width", row.length <= 40);
}

// ponytail: formatAge clamps a future createdAt (clock skew) to "0s ago" —
// was "-5s ago" when ts > Date.now(). Verified via render (formatAge is private).
{
	const futureTask = makeTask("t1", "in_progress");
	(futureTask as any).createdAt = Date.now() + 5_000; // 5s in the future
	const { comp } = makeComponent([futureTask]);
	const lines = comp.render(80);
	const ageLine = lines.find((l: string) => l.includes("ago")) ?? "";
	check("future createdAt clamps to 0s ago (not negative)", ageLine.includes("0s ago") && !ageLine.includes("-"));
}

// empty list — pageDown/end/G must not produce a negative cursorIdx (phantom cursor)
// ponytail: Math.min(tasks.length-1, …) on empty list = Math.min(-1, …) = -1 without the floor.
{
	const { comp } = makeComponent([]);
	comp.handleInput(PAGEDOWN);
	check("empty list pageDown cursor >= 0", comp.cursorIdx >= 0);
	comp.handleInput(END);
	check("empty list end cursor >= 0", comp.cursorIdx >= 0);
	comp.handleInput("G");
	check("empty list G cursor >= 0", comp.cursorIdx >= 0);
	// render must not crash / show a phantom cursor row beyond "No tasks"
	const lines = comp.render(80);
	check("empty list renders No tasks", lines.some((l: string) => l.includes("No tasks")));
	// ponytail: empty list must hint the submit path — a first-run user opening the
	// list to "No tasks" had no cue how tasks get in.
	check("empty list hints /uc submit", lines.some((l: string) => l.includes("/uc submit")));
	// ponytail: S6 — Enter/y on empty list must flash "no task selected" instead
	// of silent no-op. Mirrors the subtask-tree's Enter empty-list feedback.
	comp.handleInput(ENTER);
	check("empty list Enter flashes 'no task selected'", comp.flashMsg !== null && comp.flashMsg.includes("no task selected"));
	comp.handleInput("y");
	check("empty list y flashes 'no task selected'", comp.flashMsg !== null && comp.flashMsg.includes("no task selected"));
}

// ponytail: a 0-subtask task must NOT render "0/0" — that read as "0 completed"
// instead of "no subtasks". Mirrors formatTaskDetail's header countTag guard.
{
	const { comp } = makeComponent([makeTask("t1", "in_progress", 0)]);
	const lines = comp.render(80);
	const row = lines.find((l: string) => l.includes("t1")) ?? "";
	check("0-subtask task row has no 0/0 count", !row.includes("0/0"));
	// a task WITH subtasks still shows the count
	const { comp: comp2 } = makeComponent([makeTask("t2", "in_progress", 3)]);
	const row2 = comp2.render(80).find((l: string) => l.includes("t2")) ?? "";
	check("task with subtasks shows completed/total", row2.includes("/3"));
}

// ── search/filter tests ──────────────────────────────────────────

// `/` enters filter mode (render shows `/ ` input line with cursor)
{
	const { comp } = makeComponent([makeTask("t1", "in_progress")]);
	comp.handleInput("/");
	const lines = comp.render(80);
	check("/ enters filter mode (input line visible)", lines.some((l: string) => l.includes("/ ") && l.includes("▏")));
}

// typing narrows: 2 tasks "alpha-done" / "beta-failed", type "bet" → only beta visible
{
	const { comp } = makeComponent([
		makeTask("alpha", "completed"),
		makeTask("beta", "failed"),
	]);
	comp.handleInput("/");  // enter filter mode
	comp.handleInput("b");
	comp.handleInput("e");
	comp.handleInput("t");
	comp.handleInput(ENTER); // Enter: exit editing, keep filter
	const lines = comp.render(80);
	// Only the "beta" task row should be present, not "alpha"
	check("typing narrows to beta only", lines.some((l: string) => l.includes("beta")) && !lines.some((l: string) => l.includes("alpha")));
	check("filter header shows filtered count", lines.some((l: string) => l.includes("filtered from 2")));
}

// ponytail: command keys (y copy) fall through in search-edit — the printable
// handler swallowed ALL printable chars, so `y` appended to the query instead of
// yanking the cursor task's id. Now excluded so it exits editing + acts.
{
	const copied: string[] = [];
	const { comp } = makeComponent([makeTask("t1", "in_progress")], {
		copy: (t) => { copied.push(t); return true; },
	});
	comp.handleInput("/");
	comp.handleInput("y"); // command key, must NOT append to query
	check("y in search-edit exits searchMode", comp.searchMode === false);
	check("y in search-edit did not append to query", comp.query === "");
	check("y in search-edit copies cursor task id", copied.length === 1 && copied[0] === "t1");
}
// a plain printable char still appends (regression guard)
{
	const { comp } = makeComponent([makeTask("t1", "in_progress")]);
	comp.handleInput("/");
	comp.handleInput("z");
	check("printable 'z' still appends to query", comp.query === "z" && comp.searchMode === true);
}

// ponytail: filter matches controlState — a paused task keeps status="in_progress"
// and only flips controlState="paused". Filtering "paused" (the row badge the user
// sees) must surface it; pre-fix it returned no matches despite paused tasks existing.
{
	const paused = {
		id: "p1", description: "paused work", status: "in_progress", controlState: "paused",
		createdAt: 0, error: undefined,
		subtasks: Array.from({ length: 2 }, (_, i) => ({ id: `p1-s${i}`, description: `s${i}`, status: "pending", dependsOn: [], result: undefined, error: undefined, review: undefined, retryCount: 0, dispatchMode: "prefer_remote" })),
	} as unknown as TaskState;
	const running = makeTask("r1", "in_progress");
	const { comp } = makeComponent([paused, running]);
	comp.handleInput("/");
	comp.handleInput("p");
	comp.handleInput("a");
	comp.handleInput("u");
	comp.handleInput("s");
	comp.handleInput("e");
	comp.handleInput("d");
	comp.handleInput(ENTER);
	const lines = comp.render(80);
	check("filter 'paused' matches the paused task", lines.some((l: string) => l.includes("p1")) && !lines.some((l: string) => l.includes("r1")));
}

// Esc exits filter + restores full list
{
	const { comp } = makeComponent([
		makeTask("alpha", "completed"),
		makeTask("beta", "failed"),
	]);
	comp.handleInput("/");
	comp.handleInput("b");
	comp.handleInput("e");
	comp.handleInput("t");
	comp.handleInput(ENTER); // Enter: exit editing, keep filter
	// Now Esc should clear the filter (not close the overlay)
	comp.handleInput(ESC);
	const lines = comp.render(80);
	check("Esc restores full list (both visible)", lines.some((l: string) => l.includes("alpha")) && lines.some((l: string) => l.includes("beta")));
	check("Esc clears filter (no 'filtered from')", !lines.some((l: string) => l.includes("filtered from")));
}

// ponytail: clearing the filter restores cursor to the pre-filter cursor task
// (if still present), instead of snapping to row 0. Was cursorIdx = 0 on clear.
{
	const { comp } = makeComponent([
		makeTask("t0", "in_progress"),
		makeTask("t1", "in_progress"),
		makeTask("t2", "in_progress"),
	]);
	comp.handleInput(DOWN); // cursor → t1 (idx 1)
	comp.handleInput(DOWN); // cursor → t2 (idx 2)
	comp.handleInput("/"); // capture preFilterCursorTaskId = t2
	comp.handleInput("t0"); // filter "t0" → only t0 visible, cursor clamps to 0
	comp.handleInput(ENTER); // exit editing, keep filter
	comp.handleInput(ESC); // clear filter → restore cursor to t2
	check("filter clear restores cursor to pre-filter task (t2)", comp.cursorIdx === 2);

	// pre-filter task no longer present (evicted mid-filter) → fall back to row 0
	const { comp: comp2 } = makeComponent([makeTask("a", "in_progress"), makeTask("b", "in_progress")]);
	comp2.handleInput(DOWN); // cursor → b (idx 1)
	comp2.handleInput("/"); // capture = b
	comp2.handleInput("a"); // filter "a" → only a
	comp2.handleInput(ENTER);
	comp2.handleInput(ESC); // clear filter, b still present → restore to b
	check("filter clear restores to b (still present)", comp2.cursorIdx === 1);
}

// Backspace drops a char
{
	const { comp } = makeComponent([
		makeTask("alpha", "completed"),
		makeTask("alphabet", "failed"),
	]);
	comp.handleInput("/");
	comp.handleInput("a");
	comp.handleInput("l");
	comp.handleInput("p");
	// Now backspace once — drops the "p"
	comp.handleInput("\x7f");
	// Still in searchMode with query="al" → both alpha and alphabet match
	const items = comp.currentTasks();
	check("backspace drops last char (query=al → 2 results)", items.length === 2);
}

// Enter keeps filter then j moves within filtered set
{
	const { comp } = makeComponent([
		makeTask("alpha", "completed"),
		makeTask("beta", "failed"),
		makeTask("gamma", "completed"),
	]);
	comp.handleInput("/");
	comp.handleInput("a"); // matches "alpha" and "gamma" (both contain "a" in description "task alpha"/"task gamma")
	comp.handleInput(ENTER); // exit editing, keep filter
	const before = comp.cursorIdx;
	comp.handleInput("j");  // move down within filtered set
	check("nav within filtered set moves cursor", comp.cursorIdx === before + 1);
}

// `/` in detail mode gives feedback (flashMsg) instead of silent no-op.
// ponytail: S4 — detail is a single-task view, no list to filter; `/` tells the
// user why instead of being silently ignored. Must NOT enter searchMode.
{
	const { comp } = makeComponent([makeTask("t1", "in_progress")]);
	comp.handleInput(ENTER); // open detail
	check("detail mode active", comp.detailTaskId === "t1");
	comp.handleInput("/");   // `/` in detail mode — now sets flashMsg
	check("/ in detail mode stays in detail", comp.detailTaskId === "t1");
	check("/ in detail mode does not enter searchMode", comp.searchMode === false);
	check("/ in detail mode sets 'filter not available' flashMsg",
		comp.flashMsg !== null && comp.flashMsg.includes("filter not available"));
	const lines = comp.render(80);
	check("detail still renders subtasks header", lines.some((l: string) => l.includes("Subtasks")));
}

// Empty filtered result → "no match" line
{
	const { comp } = makeComponent([makeTask("alpha", "completed")]);
	comp.handleInput("/");
	comp.handleInput("z");
	comp.handleInput("z");
	comp.handleInput("z");
	comp.handleInput(ENTER); // exit editing, keep filter "zzz" → no match
	const lines = comp.render(80);
	check("empty filtered shows 'no match' line", lines.some((l: string) => l.includes("no match for")));
}

// height-adaptive page size: a 24-row terminal reserves 12 rows for chrome, so
// maxVisible = 12, not the legacy hardcoded 20 that the maxHeight:"100%" clamp
// would silently truncate (cutting the footer + bottom cursor rows).
// ponytail: overlayPageSize reads tui.terminal.rows; undefined tui -> fallback 20.
{
	const tasks = Array.from({ length: 50 }, (_, i) => makeTask(`t${i}`, "in_progress"));
	const factory = createTaskListOverlay({
		tasks: () => tasks,
		getTask: (id) => tasks.find((t) => t.id === id),
		onClose: () => {},
	});
	const tui = { terminal: { rows: 24 } };
	const comp = factory(tui as any, theme, undefined, () => {}) as any;
	check("24-row terminal page size = 12", comp.maxVisible === 12);
	const lines = comp.render(80);
	// header(1) + hint(1) + blank(1) + 12 rows + footer(1) = 16
	check("24-row terminal shows 12 task rows", lines.length === 16);
}

// ponytail: c/p/r quick actions — pause/resume fire immediately; cancel needs a
// double-tap (first arms via flashMsg, second fires onAction).
{
	const calls: [string, string][] = [];
	const { comp } = makeComponent(
		[makeTask("t1", "in_progress"), makeTask("t2", "in_progress")],
		{ onAction: (id, action) => { calls.push([id, action]); return true; } },
	);
	comp.handleInput(DOWN); // cursor on t2
	comp.handleInput("p");
	check("`p` pauses cursor task immediately", calls.length === 1 && calls[0][0] === "t2" && calls[0][1] === "pause");
	check("`p` sets paused flashMsg", comp.flashMsg !== null && comp.flashMsg.includes("paused"));
	comp.handleInput("r");
	check("`r` resumes cursor task immediately", calls.length === 2 && calls[1][1] === "resume");
	check("`r` sets resumed flashMsg", comp.flashMsg !== null && comp.flashMsg.includes("resumed"));
}

// double-tap cancel: first `c` arms (no onAction), second `c` fires cancel.
{
	const calls: [string, string][] = [];
	const { comp } = makeComponent(
		[makeTask("t1", "in_progress")],
		{ onAction: (id, action) => { calls.push([id, action]); return true; } },
	);
	comp.handleInput("c");
	check("first `c` arms cancel (no fire)", calls.length === 0);
	check("first `c` sets flashMsg", comp.flashMsg !== null && comp.flashMsg.includes("cancel"));
	comp.handleInput("c");
	check("second `c` fires cancel", calls.length === 1 && calls[0][1] === "cancel");
	// ponytail: second `c` now sets a confirmation flashMsg ("cancelled …") instead
	// of clearing to null — the user sees the action landed.
	check("second `c` sets cancelled flashMsg", comp.flashMsg !== null && comp.flashMsg.includes("cancelled"));
}

// ponytail: cancel armed flash shows the FULL task id (cancel is destructive — a
// truncated prefix risks confirming the wrong task when multiple share a prefix).
{
	const longId = "task_with_a_long_id_1234567890";
	const { comp } = makeComponent(
		[makeTask(longId, "in_progress")],
		{ onAction: () => true },
	);
	comp.handleInput("c"); // arm
	check("armed flash shows full id", comp.flashMsg !== null && comp.flashMsg.includes(longId));
}

// ponytail: fireAction success flash shows the FULL task id (was slice(0,8)) —
// mirrors the cancel-armed full-id fix; multiple tasks can share an 8-char prefix.
{
	const longId = "task_with_a_long_id_abc123";
	const { comp } = makeComponent(
		[makeTask(longId, "in_progress")],
		{ onAction: () => true },
	);
	comp.handleInput("c"); // arm
	comp.handleInput("c"); // fire → "cancelled <full id>"
	check("fireAction success flash shows full id", comp.flashMsg !== null && comp.flashMsg.includes(longId));
}

// double-tap abort: first `c` arms, a nav key clears without firing.
{
	const calls: [string, string][] = [];
	const { comp } = makeComponent(
		[makeTask("t1", "in_progress"), makeTask("t2", "in_progress")],
		{ onAction: (id, action) => { calls.push([id, action]); return true; } },
	);
	comp.handleInput("c");
	comp.handleInput(DOWN); // any non-c key aborts
	check("nav after armed `c` aborts (no fire)", calls.length === 0);
	check("nav clears flashMsg", comp.flashMsg === null);
}

// ponytail: cancel dead-key guard for terminal statuses — `c` on a completed/
// cancelled task must NOT arm the double-tap (the server would reject the cancel
// anyway). Flashes "already <status>" instead. `failed` is left to the server.
// ponytail: resume dead-key guard for terminal statuses — `r` on a completed/
// cancelled task must NOT fire resume (the server would reject it). Flashes
// "already <status>" instead. Mirrors the cancel terminal-status guard.
// ponytail: pause dead-key guard for terminal statuses — `p` on a completed/
// cancelled task must NOT fire pause (server would reject). Flashes "already
// <status>". Mirrors the cancel/resume terminal-status guards (closes the trio).
{
	const calls: [string, string][] = [];
	const { comp } = makeComponent(
		[makeTask("t1", "completed"), makeTask("t2", "cancelled")],
		{ onAction: (id, action) => { calls.push([id, action]); return true; } },
	);
	comp.handleInput("c"); // cursor on t1 (completed)
	check("c on completed does not arm", comp.pendingCancel === null);
	check("c on completed does not fire", calls.length === 0);
	check("c on completed flashes 'already completed'", comp.flashMsg !== null && comp.flashMsg.includes("already completed"));
	// second c on completed still must not fire (no armed state)
	comp.handleInput("c");
	check("second c on completed still does not fire", calls.length === 0);

	comp.handleInput(DOWN); // cursor → t2 (cancelled)
	comp.handleInput("c");
	check("c on cancelled flashes 'already cancelled'", comp.flashMsg !== null && comp.flashMsg.includes("already cancelled"));
	check("c on cancelled does not fire", calls.length === 0);
}

// ponytail: detail-mode cancel guard — terminal status flashes "already <status>"
// instead of firing. getTask wired (makeComponent wires it).
{
	const calls: [string, string][] = [];
	const { comp } = makeComponent(
		[makeTask("t1", "completed")],
		{ onAction: (id, action) => { calls.push([id, action]); return true; } },
	);
	comp.handleInput(ENTER); // open detail on t1
	comp.handleInput("c");
	check("detail c on completed does not fire", calls.length === 0);
	check("detail c on completed flashes 'already completed'", comp.flashMsg !== null && comp.flashMsg.includes("already completed"));
}

// ponytail: armed double-tap must not survive a cursor round-trip — arm t1,
// nav to t2, nav BACK to t1, then `c` must NOT fire (the user moved away;
// intent changed). Stale pendingCancel===t1 would re-match and fire cancel t1.
{
	const calls: [string, string][] = [];
	const { comp } = makeComponent(
		[makeTask("t1", "in_progress"), makeTask("t2", "in_progress")],
		{ onAction: (id, action) => { calls.push([id, action]); return true; } },
	);
	comp.handleInput("c"); // arm t1 (cursor 0)
	comp.handleInput(DOWN); // → t2 (cursor 1), aborts armed
	comp.handleInput(UP); // → t1 (cursor 0)
	comp.handleInput("c"); // pendingCancel cleared by DOWN → arms t1 fresh, NOT fires
	check("round-trip: first c back on t1 does not fire (re-arm)", calls.length === 0);
	check("round-trip: pendingCancel re-armed to t1", comp.pendingCancel === "t1");
	comp.handleInput("c"); // second c now fires
	check("round-trip: explicit second c fires cancel t1", calls.length === 1 && calls[0][0] === "t1" && calls[0][1] === "cancel");
}

// ponytail: resume dead-key guard — list `r` on completed/cancelled (no fire).
{
	const calls2: [string, string][] = [];
	const { comp } = makeComponent(
		[makeTask("t1", "completed"), makeTask("t2", "cancelled")],
		{ onAction: (id, action) => { calls2.push([id, action]); return true; } },
	);
	comp.handleInput("r"); // cursor on t1 (completed)
	check("r on completed does not fire", calls2.length === 0);
	check("r on completed flashes 'already completed'", comp.flashMsg !== null && comp.flashMsg.includes("already completed"));
	comp.handleInput(DOWN); // → t2 (cancelled)
	comp.handleInput("r");
	check("r on cancelled flashes 'already cancelled'", comp.flashMsg !== null && comp.flashMsg.includes("already cancelled"));
	check("r on cancelled does not fire", calls2.length === 0);

	// detail-mode `r` guard (single-tap)
	const { comp: d2 } = makeComponent(
		[makeTask("t1", "completed")],
		{ onAction: (id, action) => { calls2.push([id, action]); return true; } },
	);
	d2.handleInput(ENTER); // open detail on t1
	d2.handleInput("r");
	check("detail r on completed does not fire", calls2.length === 0);
	check("detail r on completed flashes 'already completed'", d2.flashMsg !== null && d2.flashMsg.includes("already completed"));
}

// ponytail: pause dead-key guard — list `p` on completed/cancelled (no fire).
{
	const calls3: [string, string][] = [];
	const { comp } = makeComponent(
		[makeTask("t1", "completed"), makeTask("t2", "cancelled")],
		{ onAction: (id, action) => { calls3.push([id, action]); return true; } },
	);
	comp.handleInput("p"); // cursor on t1 (completed)
	check("p on completed does not fire", calls3.length === 0);
	check("p on completed flashes 'already completed'", comp.flashMsg !== null && comp.flashMsg.includes("already completed"));
	comp.handleInput(DOWN); // → t2 (cancelled)
	comp.handleInput("p");
	check("p on cancelled flashes 'already cancelled'", comp.flashMsg !== null && comp.flashMsg.includes("already cancelled"));
	check("p on cancelled does not fire", calls3.length === 0);

	// detail-mode `p` guard
	const { comp: d3 } = makeComponent(
		[makeTask("t1", "cancelled")],
		{ onAction: (id, action) => { calls3.push([id, action]); return true; } },
	);
	d3.handleInput(ENTER);
	d3.handleInput("p");
	check("detail p on cancelled does not fire", calls3.length === 0);
	check("detail p on cancelled flashes 'already cancelled'", d3.flashMsg !== null && d3.flashMsg.includes("already cancelled"));
}

// initialDetailTaskId (jump-from-subtask-tree) — opens straight into detail mode.
{
	const { comp } = makeComponent(
		[makeTask("t1", "in_progress"), makeTask("t2", "in_progress")],
		{ initialDetailTaskId: "t2" },
	);
	check("initialDetailTaskId opens in detail mode", comp.detailTaskId === "t2");
}

// ponytail: S11 — jump-from-subtask-tree opens detail via initialDetailTaskId
// with cursorIdx at 0; Esc back to list must land the cursor on the jumped-to
// task (t2 at idx 1), not the first task. Without the openDetail cursor-align,
// a subsequent c/p/r would hit t1, not the task the user just inspected.
{
	const { comp } = makeComponent(
		[makeTask("t1", "in_progress"), makeTask("t2", "in_progress"), makeTask("t3", "in_progress")],
		{ initialDetailTaskId: "t2" },
	);
	check("jump lands in detail for t2", comp.detailTaskId === "t2");
	comp.handleInput(ESC); // back to list
	check("esc returns to list", comp.detailTaskId === null);
	check("list cursor aligned to jumped task (t2@idx1)", comp.cursorIdx === 1);
	// verify the rendered cursor row is on t2, not t1
	const lines = comp.render(80) as string[];
	check("rendered cursor is on t2", lines.some((l: string) => l.includes("t2") && l.includes("›")));
	check("rendered cursor is NOT on t1", !lines.some((l: string) => l.includes("t1") && l.includes("›")));
	// a detail open via Enter (cursor already aligned) must keep alignment
	comp.handleInput(ENTER);
	comp.handleInput(ESC);
	check("re-enter+esc keeps cursor on t2", comp.cursorIdx === 1);
}

// ponytail: S3 — detail-mode quick actions (c/p/r) fire on the detail's own task.
// Single-tap (no double-tap confirm): detail is a focused single-task view.
{
	const calls: [string, string][] = [];
	const { comp } = makeComponent(
		[makeTask("t1", "in_progress"), makeTask("t2", "in_progress")],
		{ onAction: (id, action) => { calls.push([id, action]); return true; } },
	);
	comp.handleInput(ENTER); // open detail for t1 (cursor at index 0)
	check("detail open for t1", comp.detailTaskId === "t1");
	comp.handleInput("p");
	check("detail `p` fires pause on detail task", calls.length === 1 && calls[0][0] === "t1" && calls[0][1] === "pause");
	check("detail `p` sets paused flashMsg", comp.flashMsg !== null && comp.flashMsg.includes("paused"));
	comp.handleInput("r");
	check("detail `r` fires resume on detail task", calls.length === 2 && calls[1][0] === "t1" && calls[1][1] === "resume");
	check("detail `r` sets resumed flashMsg", comp.flashMsg !== null && comp.flashMsg.includes("resumed"));
	comp.handleInput("c");
	check("detail `c` fires cancel immediately (no double-tap)", calls.length === 3 && calls[2][0] === "t1" && calls[2][1] === "cancel");
	check("detail `c` sets cancelled flashMsg", comp.flashMsg !== null && comp.flashMsg.includes("cancelled"));
	// detail `c` must NOT arm pendingCancel (single-tap, not double-tap)
	check("detail `c` does not arm pendingCancel", comp.pendingCancel === null);
}

// ponytail: S5 — narrow-screen hint. Full hint (~78 chars) gets ANSI-truncated
// on terminals < 78 cols, losing the right side (Esc close, / filter). The
// renderList hint uses a compact version under 60 cols. Detail hint (~50 chars)
// is left as-is (fits most widths). Only the NORMAL (non-search, non-filtering)
// hint branch is affected.
{
	const { comp } = makeComponent([makeTask("t1", "in_progress"), makeTask("t2", "in_progress")]);
	const wide = comp.render(80).join("\n");
	check("wide hint has PgUp/PgDn", wide.includes("PgUp/PgDn"));
	check("wide hint has / filter", wide.includes("/ filter"));
	check("wide hint has Esc close", wide.includes("Esc close"));
	// ponytail: list `c` is a double-tap confirm, not single-tap. The hint must
	// signal "(2×)" so a first press (which arms, not cancels) isn't read as a
	// dead key. Was bare "c cancel" — misleading.
	check("wide hint marks cancel as 2× confirm", wide.includes("c cancel (2×)"));
	// ponytail: N is prev-failed (complement of n next-failed). `p` is pause, so
	// the hint must advertise N or the prev-failure path stays undiscoverable.
	check("wide hint has N prev-failed", wide.includes("N prev-failed"));

	const narrow = comp.render(50).join("\n");
	check("narrow hint has c/p/r", narrow.includes("c/p/r"));
	check("narrow hint has n/N failed", narrow.includes("n/N failed"));
	check("narrow hint has Esc close", narrow.includes("Esc close"));
	check("narrow hint does NOT have PgUp/PgDn", !narrow.includes("PgUp/PgDn"));
}

// ponytail: detail hint must advertise y/Y copy — the list hint shows it, and
// detail mode implements both (y = task id, Y = error), but the detail hint
// omitted it, so the yank path was undiscoverable in the focused view.
{
	const { comp } = makeComponent([makeTask("t1", "in_progress")]);
	comp.handleInput(ENTER);
	const lines = comp.render(80) as string[];
	const detail = lines.find((l: string) => l.includes("scroll"));
	check("detail hint advertises y/Y copy", detail !== undefined && detail.includes("y/Y copy"));
}

// ponytail: F1 — detail mode must RENDER flashMsg, not just set state. Before F1,
// renderDetail() omitted the flashMsg line, so `/` and c/p/r feedback in detail
// mode was invisible even though the state was set (S3/S4 selfchecks only asserted
// state — the render-layer regression slipped through).
{
	const { comp } = makeComponent([makeTask("t1", "in_progress")]);
	comp.handleInput(ENTER);
	comp.handleInput("/");
	check("F1 state: detail `/` sets flashMsg", comp.flashMsg !== null);
	const lines = comp.render(80) as string[];
	check("F1 render: detail output contains flashMsg text",
		lines.some((l: string) => l.includes("filter not available")));
}

// ponytail: F2 — failed action must replace "cancelling…" with "${action} failed".
// Sync false and Promise false both covered.
{
	const { comp } = makeComponent(
		[makeTask("t1", "in_progress")],
		{ onAction: () => false }, // sync failure
	);
	comp.handleInput("c"); comp.handleInput("c"); // double-tap fires cancel
	check("F2 sync false sets 'cancel failed'", comp.flashMsg !== null && comp.flashMsg.includes("cancel failed"));

	const { comp: comp2 } = makeComponent(
		[makeTask("t1", "in_progress")],
		{ onAction: () => Promise.resolve(false) }, // async failure
	);
	comp2.handleInput("p");
	await new Promise((r) => setTimeout(r, 10));
	check("F2 promise false sets 'pause failed'", comp2.flashMsg !== null && comp2.flashMsg.includes("pause failed"));
}

// ponytail: F3 — p/r without onAction are no longer silent dead keys (list mode
// mirrors c's "cancel unavailable"; detail mode c/p/r surface via fireAction).
{
	const { comp } = makeComponent([makeTask("t1", "in_progress")]); // no onAction
	comp.handleInput("p");
	check("F3 list `p` w/o onAction flashes unavailable", comp.flashMsg !== null && comp.flashMsg.includes("pause unavailable"));
	comp.handleInput("r");
	check("F3 list `r` w/o onAction flashes unavailable", comp.flashMsg !== null && comp.flashMsg.includes("resume unavailable"));
	comp.handleInput(ENTER); // detail mode, still no onAction
	comp.handleInput("c");
	check("F3 detail `c` w/o onAction flashes unavailable", comp.flashMsg !== null && comp.flashMsg.includes("cancel unavailable"));
	const lines = comp.render(80) as string[];
	check("F3 detail unavailable flash is rendered (F1 pair)",
		lines.some((l: string) => l.includes("cancel unavailable")));
}

// ponytail: F5 — unknown status badge renders "?" (4-wide column), not "plan",
// so a newer server's status can't masquerade as planning.
{
	const t = makeTask("t1", "in_progress");
	(t as any).status = "some_future_status";
	const { comp } = makeComponent([t]);
	const lines = comp.render(80) as string[];
	check("F5 unknown badge renders ?", lines.some((l: string) => l.includes("?")));
	check("F5 unknown badge not 'plan'", !lines.some((l: string) => l.includes("plan")));
}

// ponytail: paused task badge — pauseTask sets controlState="paused" but leaves
// status="in_progress". The list row must render the "hold" badge (via the
// paused entry), not "run " (running). STATUS_BADGE.paused was dead code before
// this because statusBadge was called with task.status, which never becomes
// "paused" (only controlState does).
{
	const t = makeTask("t1", "in_progress");
	(t as any).controlState = "paused";
	const { comp } = makeComponent([t]);
	const lines = comp.render(80) as string[];
	check("paused: list row shows 'hold' badge", lines.some((l: string) => l.includes("hold")));
	check("paused: list row does NOT show 'run' badge", !lines.some((l: string) => l.includes("run ")));
	// running controlState (normal) stays "run "
	const t2 = makeTask("t2", "in_progress");
	const { comp: comp2 } = makeComponent([t2]);
	const lines2 = comp2.render(80) as string[];
	check("running: list row shows 'run' badge", lines2.some((l: string) => l.includes("run ")));
}


// convey that content ABOVE is hidden once scrollOffset > 0.
{
	const tui = { terminal: { rows: 24 } }; // maxVisible = 12
	const tasks = Array.from({ length: 50 }, (_, i) => makeTask(`t${i}`, "in_progress"));
	const factory = createTaskListOverlay({ tasks: () => tasks, getTask: () => undefined, onClose: () => {} });
	const comp = factory(tui as any, theme, undefined, () => {}) as any;
	const footer = () => (comp.render(80) as string[]).find((l: string) => l.includes("of 50"));
	check("F6 at top: ▼ below, no ▲", footer()?.includes("▼") === true && footer()?.includes("▲") === false);
	comp.handleInput(PAGEDOWN); // cursor 12 → scrollOffset 1
	check("F6 mid-scroll: both ▲ and ▼", footer()?.includes("▲") === true && footer()?.includes("▼") === true);
	comp.handleInput("G"); // bottom
	check("F6 at bottom: ▲ above, no ▼", footer()?.includes("▲") === true && footer()?.includes("▼") === false);
}

// ponytail: F7 — refresh timer ticks requestRender while open; dispose() stops
// it (leaked timers would keep re-rendering a closed overlay + pin the process).
{
	let ticks = 0;
	const mockTui = { requestRender: () => { ticks++; } };
	const comp = createTaskListOverlay({
		tasks: () => [makeTask("t1", "in_progress")],
		getTask: () => undefined, onClose: () => {},
	})(mockTui as any, theme, undefined, () => {}) as any;
	await new Promise((r) => setTimeout(r, 1100));
	check("F7 timer ticks requestRender", ticks >= 1);
	comp.dispose();
	const after = ticks;
	await new Promise((r) => setTimeout(r, 1100));
	check("F7 dispose stops timer", ticks === after);
}

// ponytail: F22 — yank keys with injectable copier (no real clipboard touched).
{
	const copied: string[] = [];
	const tasks = [makeTask("t1", "in_progress"), makeTask("t2", "in_progress")];
	const mk = (copy: (t: string) => boolean) => createTaskListOverlay({
		tasks: () => tasks,
		getTask: (id) => tasks.find((t) => t.id === id),
		copy, onClose: () => {},
	})(undefined, theme, undefined, () => {}) as any;
	const comp = mk((t) => { copied.push(t); return true; });
	comp.handleInput(DOWN); // cursor → t2
	comp.handleInput("y");
	check("F22 list `y` copies cursor task id", copied.length === 1 && copied[0] === "t2");
	check("F22 list `y` flashes copied", comp.flashMsg !== null && comp.flashMsg.includes("copied"));
	comp.handleInput(ENTER); // detail for t2 (cursor aligned)
	comp.handleInput("y");
	check("F22 detail `y` copies detail task id", copied.length === 2 && copied[1] === "t2");
	const comp2 = mk(() => false);
	comp2.handleInput("y");
	check("F22 copy failure flashes 'copy failed'", comp2.flashMsg !== null && comp2.flashMsg.includes("copy failed"));
}

// ponytail: `Y` yanks the cursor task's error text (mirrors subtask-tree `Y`).
// No error → "no error to copy" flash. Detail-mode `Y` reads via getTask.
{
	const copied: string[] = [];
	const tasks = [
		Object.assign(makeTask("t1", "failed"), { error: "task blew up" }),
		makeTask("t2", "in_progress"),
	] as TaskState[];
	const mk = (copy: (t: string) => boolean) => createTaskListOverlay({
		tasks: () => tasks,
		getTask: (id) => tasks.find((t) => t.id === id),
		copy, onClose: () => {},
	})(undefined, theme, undefined, () => {}) as any;
	const comp = mk((t) => { copied.push(t); return true; });
	comp.handleInput("Y"); // cursor on t1 (failed, has error)
	check("F22 list `Y` copies task error", copied.length === 1 && copied[0] === "task blew up");
	comp.handleInput(DOWN); // → t2 (no error)
	comp.handleInput("Y");
	check("F22 list `Y` no error flashes", comp.flashMsg !== null && comp.flashMsg.includes("no error to copy"));
	check("F22 list `Y` no error copies nothing", copied.length === 1);

	// detail-mode `Y`
	comp.handleInput(UP); // → t1
	comp.handleInput(ENTER);
	comp.handleInput("Y");
	check("F22 detail `Y` copies task error", copied.length === 2 && copied[1] === "task blew up");
}

// ponytail: regression guard — yank flash shows the FULL id, not a truncated
// prefix. Was slice(0,8), which rendered `copied task_abc1` for a long id and
// hid what actually landed on the clipboard. A long id must appear verbatim.
{
	const copied: string[] = [];
	const longId = "task_01J9X4F2K7M3QZ8ABC";
	const tasks = [makeTask(longId, "in_progress")];
	const comp = createTaskListOverlay({
		tasks: () => tasks, getTask: (id) => tasks.find((t) => t.id === id),
		copy: (t) => { copied.push(t); return true; }, onClose: () => {},
	})(undefined, theme, undefined, () => {}) as any;
	comp.handleInput("y");
	check("F22 long-id yank flash shows full id", comp.flashMsg === `copied ${longId}`);
	check("F22 long-id copies full id verbatim", copied.length === 1 && copied[0] === longId);
}

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
// ponytail: explicit exit — overlay components start 1s refresh timers; without
// exit(0) the pending intervals keep the script alive after ALL PASS.
process.exit(failures === 0 ? 0 : 1);
