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
	// no getter + no details.task → header only (graceful degradation for evicted tasks)
	const bare = createTaskResultRenderer();
	const bareLines = (bare(msg, { expanded: true }, theme) as any).render(80) as string[];
	check("F15 no task source degrades to header only", bareLines.length === 1);
}

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
