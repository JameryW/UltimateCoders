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
}) {
	const task = {
		id: "T", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks,
	} as unknown as TaskState;
	const factory = createSubtaskTreeOverlay({
		tasks: () => [task],
		onRetry: opts?.onRetry ?? (() => {}),
		onJumpToTask: opts?.onJumpToTask,
		cursorOnFailed: opts?.cursorOnFailed,
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

// `d` jump — fires onJumpToTask with the subtask's taskId, then closes the tree.
{
	let jumpedTo: string | null = null;
	const { comp, closed } = makeComponent(
		[makeSubtask("s0", { status: "failed" })],
		{ onJumpToTask: (taskId) => { jumpedTo = taskId; } },
	);
	comp.handleInput("d");
	check("`d` fires onJumpToTask with parent taskId", jumpedTo === "T");
	check("`d` closes the tree (done())", closed() === true);
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

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
// ponytail: explicit exit — overlay components start 1s refresh timers; without
// exit(0) the pending intervals keep the script alive after ALL PASS.
process.exit(failures === 0 ? 0 : 1);
