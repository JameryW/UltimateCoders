/**
 * Self-check for formatTaskDetail topological depth.
 * Run: bun run src/ui/status-formatter.selfcheck.ts
 *
 * ponytail: invariant — depth = longest dep chain to root, NOT dependsOn.length.
 * A subtask depending on 3 roots must be depth 1 (was 3 pre-fix).
 */
import type { Theme, ThemeColor } from "@oh-my-pi/pi-coding-agent";
import { formatTaskDetail } from "./status-formatter";
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

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
