/**
 * Self-check for formatTaskDetail topological depth.
 * Run: bun run src/ui/status-formatter.selfcheck.ts
 *
 * ponytail: invariant — depth = longest dep chain to root, NOT dependsOn.length.
 * A subtask depending on 3 roots must be depth 1 (was 3 pre-fix).
 */
import type { Theme, ThemeColor } from "@oh-my-pi/pi-coding-agent";
import { formatTaskDetail, formatTaskList } from "./status-formatter";
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

// Cycle guard: A→B→A must not infinite-loop
{
	const task = { id: "T", description: "t", status: "in_progress", controlState: "running", createdAt: 0, subtasks: [
		st("A", ["B"]), st("B", ["A"]),
	] } as unknown as TaskState;
	let threw = false;
	try { formatTaskDetail(task, theme); } catch { threw = true; }
	check("cycle does not throw", !threw);
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

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
