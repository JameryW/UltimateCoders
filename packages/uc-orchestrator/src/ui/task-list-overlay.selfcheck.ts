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
}) {
	const factory = createTaskListOverlay({
		tasks: () => tasks,
		getTask: (id) => tasks.find((t) => t.id === id),
		onAction: opts?.onAction,
		initialDetailTaskId: opts?.initialDetailTaskId,
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

	const narrow = comp.render(50).join("\n");
	check("narrow hint has c/p/r", narrow.includes("c/p/r"));
	check("narrow hint has Esc close", narrow.includes("Esc close"));
	check("narrow hint does NOT have PgUp/PgDn", !narrow.includes("PgUp/PgDn"));
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

// ponytail: F6 — scroll footer arrows mark the clipped side. Bare counts don't
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

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
