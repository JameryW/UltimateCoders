/**
 * Self-check for TaskResultRenderer width handling.
 * Run: bun run src/ui/task-result-renderer.selfcheck.ts
 *
 * ponytail: invariant — render(width) respects terminal width (was a
 * hardcoded slice(0,80) ignoring width, overflowing narrow terminals).
 */
import type { Theme, ThemeColor } from "@oh-my-pi/pi-coding-agent";
import type { TaskState, SubtaskResult } from "../orchestrator/orchestrator";
import { createTaskResultRenderer } from "./task-result-renderer";

const theme: Theme = {
	fg: (_c: ThemeColor, t: string) => t,
	bold: (t: string) => t,
} as unknown as Theme;

let failures = 0;
function check(name: string, cond: boolean) {
	console.log(`${cond ? "PASS" : "FAIL"}: ${name}`);
	if (!cond) failures++;
}

function makeSubtask(id: string, status: SubtaskResult["status"], desc: string, error?: string): SubtaskResult {
	return { id, description: desc, status, dependsOn: [], files: [], error } as unknown as SubtaskResult;
}

function makeMessage(expanded: boolean, subtasks: SubtaskResult[], status = "completed") {
	const task = { id: "T", description: "t", status, controlState: "running", createdAt: 0, subtasks } as unknown as TaskState;
	return { details: { taskId: "T-1234567890", status, subtaskCount: subtasks.length, task } };
}

const renderer = createTaskResultRenderer();

// collapsed — only summary header, no subtask rows
{
	const comp = renderer(makeMessage(false, [makeSubtask("s1", "completed", "x")]), { expanded: false }, theme)!;
	const lines = (comp as any).render(80) as string[];
	check("collapsed = 1 summary line", lines.length === 1);
	check("summary has task id", lines[0].includes("T-1234567"));
}

// expanded wide — full desc shown
{
	const subs = [makeSubtask("s1", "completed", "a short task"), makeSubtask("s2", "failed", "another", "boom err")];
	const comp = renderer(makeMessage(true, subs, "failed"), { expanded: true }, theme)!;
	const lines = (comp as any).render(80) as string[];
	check("expanded shows both subtasks", lines.some((l: string) => l.includes("s1")) && lines.some((l: string) => l.includes("s2")));
	check("expanded shows error", lines.some((l: string) => l.includes("boom err")));
}

// ponytail: success output — the expanded message showed a subtask's error +
// review but NOT its result (what it actually produced). Now a completed subtask
// with a result shows its first line (+ `…` when there's more). Error takes
// priority (a subtask has one or the other, not both).
{
	const done = makeSubtask("s1", "completed", "did the thing") as SubtaskResult;
	(done as any).result = "produced output line 1\nline 2";
	const comp = renderer(makeMessage(true, [done], "completed"), { expanded: true }, theme)!;
	const lines = (comp as any).render(80) as string[];
	check("expanded shows success result first line", lines.some((l: string) => l.includes("produced output line 1")));
	// multi-line result → ellipsis signals more
	check("expanded multi-line result shows ellipsis", lines.some((l: string) => l.includes("produced output line 1") && l.includes("…")));
	// single-line result that fits → no ellipsis
	const one = makeSubtask("s2", "completed", "x") as SubtaskResult;
	(one as any).result = "short output";
	const oneLines = (renderer(makeMessage(true, [one], "completed"), { expanded: true }, theme)! as any).render(80) as string[];
	check("expanded single-line result no ellipsis", oneLines.some((l: string) => l.includes("short output") && !l.includes("…")));
}

// ponytail: review verdict in the expanded completion message — the tree overlay
// + /uc status show ✓/✗ approved/rejected; the expanded message omitted it. A
// rejected subtask on a "completed" task is the actionable detail.
{
	const approved = makeSubtask("s1", "completed", "ok") as SubtaskResult;
	(approved as any).review = { approved: true };
	const rejected = makeSubtask("s2", "failed", "bad", "boom err") as SubtaskResult;
	(rejected as any).review = { approved: false };
	const comp = renderer(makeMessage(true, [approved, rejected], "failed"), { expanded: true }, theme)!;
	const lines = (comp as any).render(80) as string[];
	check("expanded shows approved verdict", lines.some((l: string) => l.includes("✓ approved")));
	check("expanded shows rejected verdict", lines.some((l: string) => l.includes("✗ rejected")));
	// no review field → no verdict line (no noise on unreviewed subtasks)
	const bare = makeSubtask("s3", "completed", "plain") as SubtaskResult;
	const bareLines = (renderer(makeMessage(true, [bare]), { expanded: true }, theme)! as any).render(80) as string[];
	check("expanded unreviewed → no verdict line", !bareLines.some((l: string) => l.includes("approved")));
}

// ponytail: review issues/suggestions in the expanded completion message —
// the verdict alone hides why a subtask was rejected. Each issue (•) and
// suggestion (↳) now gets its own dim line. Approved reviews can still carry
// suggestions, so both render regardless of verdict.
{
	const rejected = makeSubtask("s1", "failed", "bad", "boom err") as SubtaskResult;
	(rejected as any).review = { approved: false, issues: ["missing tests", "lint error"], suggestions: ["add fixture"] };
	const comp = renderer(makeMessage(true, [rejected], "failed"), { expanded: true }, theme)!;
	const lines = (comp as any).render(80) as string[];
	check("expanded shows issue #1", lines.some((l: string) => l.includes("• missing tests")));
	check("expanded shows issue #2", lines.some((l: string) => l.includes("• lint error")));
	check("expanded shows suggestion", lines.some((l: string) => l.includes("↳ add fixture")));
	// approved with suggestions still shows them
	const approved = makeSubtask("s2", "completed", "ok") as SubtaskResult;
	(approved as any).review = { approved: true, issues: [], suggestions: ["nice work"] };
	const aLines = (renderer(makeMessage(true, [approved]), { expanded: true }, theme)! as any).render(80) as string[];
	check("expanded approved shows suggestion", aLines.some((l: string) => l.includes("↳ nice work")));
	// empty issues/suggestions → no bullet/arrow lines
	const empty = makeSubtask("s3", "completed", "ok") as SubtaskResult;
	(empty as any).review = { approved: true, issues: [], suggestions: [] };
	const eLines = (renderer(makeMessage(true, [empty]), { expanded: true }, theme)! as any).render(80) as string[];
	check("expanded empty issues → no bullet", !eLines.some((l: string) => l.includes("•")));
	check("expanded empty suggestions → no arrow", !eLines.some((l: string) => l.includes("↳")));
}

// ponytail: retry×N inline on a failed expanded subtask — mirrors subtask-tree
// #439 + detail #440. A "hard" failure (N retries) is visible in the completion
// message without opening the tree. Only failed + retryCount>0; none otherwise.
{
	const retried = makeSubtask("s1", "failed", "bad", "boom") as SubtaskResult;
	(retried as any).retryCount = 3;
	const lines = (renderer(makeMessage(true, [retried], "failed"), { expanded: true }, theme)! as any).render(80) as string[];
	const row = lines.find((l: string) => l.includes("s1:")) ?? "";
	check("expanded failed row shows retry×N (3)", row.includes("retry×3"));
	// first-attempt failure (retryCount=0) → no retry tag
	const first = makeSubtask("s2", "failed", "bad", "boom") as SubtaskResult;
	const fLines = (renderer(makeMessage(true, [first], "failed"), { expanded: true }, theme)! as any).render(80) as string[];
	const fRow = fLines.find((l: string) => l.includes("s2:")) ?? "";
	check("expanded failed row no retry tag when retryCount=0", !fRow.includes("retry×"));
	// non-failed → no retry tag even with retryCount>0
	const done = makeSubtask("s3", "completed", "ok") as SubtaskResult;
	(done as any).retryCount = 2;
	const dLines = (renderer(makeMessage(true, [done]), { expanded: true }, theme)! as any).render(80) as string[];
	const dRow = dLines.find((l: string) => l.includes("s3:")) ?? "";
	check("expanded non-failed row no retry tag", !dRow.includes("retry×"));
}

// expanded narrow (width 30) — subtask desc lines truncated to fit
// (summary header is a fixed message header, OMP wraps it — not in scope)
{
	const subs = [makeSubtask("s1", "completed", "this is a very long subtask description that should truncate")];
	const comp = renderer(makeMessage(true, subs), { expanded: true }, theme)!;
	const lines = (comp as any).render(30) as string[];
	const subtaskLines = lines.filter((l: string) => l.includes("s1:"));
	check("narrow width: subtask line fits 30", subtaskLines.every((l: string) => l.length <= 30));
}

// width so small desc budget clamps to 0 — no throw, subtask id still shown
{
	const subs = [makeSubtask("s1", "completed", "desc")];
	const comp = renderer(makeMessage(true, subs), { expanded: true }, theme)!;
	let threw = false;
	let lines: string[] = [];
	try { lines = (comp as any).render(5) as string[]; } catch { threw = true; }
	check("tiny width no throw", !threw);
	check("tiny width still shows subtask id", lines.some((l: string) => l.includes("s1")));
}

// ponytail: F15 — the emitter sends only {taskId, status, subtaskCount} (no
// details.task), so the expanded view must resolve the snapshot via getter.
{
	const subs = [makeSubtask("s1", "completed", "getter task"), makeSubtask("s2", "failed", "other", "kaput")];
	const liveTask = {
		id: "T-1234567890", description: "t", status: "failed", controlState: "running",
		createdAt: 0, subtasks: subs,
	} as unknown as TaskState;
	const withGetter = createTaskResultRenderer((id) => (id === "T-1234567890" ? liveTask : undefined));
	// exactly what the emitter sends — no `task` field
	const msg = { details: { taskId: "T-1234567890", status: "failed", subtaskCount: 2 } };
	const lines = (withGetter(msg, { expanded: true }, theme) as any).render(80) as string[];
	check("F15 getter resolves expanded subtasks", lines.some((l: string) => l.includes("s1")) && lines.some((l: string) => l.includes("s2")));
	check("F15 getter shows subtask error", lines.some((l: string) => l.includes("kaput")));
	// ponytail: breakdown — "failed — 2 subtask(s) (1 failed, 1 done)" surfaces the
	// actionable count, not just the total. Was bare "2 subtask(s)".
	check("breakdown shows failed count in summary", lines[0].includes("1 failed") && lines[0].includes("1 done"));
	// no getter + no details.task → header only (graceful degradation for evicted tasks)
	const bare = createTaskResultRenderer();
	const bareLines = (bare(msg, { expanded: true }, theme) as any).render(80) as string[];
	check("F15 no task source degrades to header only", bareLines.length === 1);
	// ponytail: evicted task (no subtask data) → bare suffix, no breakdown.
	// "failed" here is the task STATUS word (always present), so check the line
	// ENDS with the bare count rather than absence of "failed".
	check("evicted task summary keeps bare subtask count", bareLines[0].endsWith("2 subtask(s)"));
}

// ponytail: all-completed task → bare suffix (no "(N done)" — the total already
// conveys it). Mixed-failed surfaces the failed count. Single-status tasks too.
{
	const mk = (subs: { id: string; status: string }[]) => subs.map((s) => makeSubtask(s.id, s.status as any, "d")) as SubtaskResult[];
	const render = (subs: { id: string; status: string }[], status: string) => {
		const t = { id: "T", description: "t", status, controlState: "running", createdAt: 0, subtasks: mk(subs) } as unknown as TaskState;
		const r = createTaskResultRenderer();
		const msg = { details: { taskId: "T-1234567890", status, subtaskCount: subs.length, task: t } };
		return (r(msg, { expanded: false }, theme) as any).render(80) as string[];
	};
	const allDone = render([{ id: "s1", status: "completed" }, { id: "s2", status: "completed" }, { id: "s3", status: "completed" }], "completed");
	// ponytail: bare suffix = "3 subtask(s)" with NOTHING after (the "(s)" plural
	// marker always contains a paren, so !includes("(") was a false negative).
	// Breakdown appends " (N failed…)" — detect by "subtask(s) (" with a digit.
	check("all-completed = bare count (no breakdown)", allDone[0].endsWith("3 subtask(s)"));
	const twoFail = render([{ id: "s1", status: "completed" }, { id: "s2", status: "failed" }, { id: "s3", status: "failed" }, { id: "s4", status: "cancelled" }], "failed");
	check("mixed shows failed+cancelled, done only when partial", twoFail[0].includes("2 failed") && twoFail[0].includes("1 cancelled") && twoFail[0].includes("1 done"));
}

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
