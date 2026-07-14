/**
 * Self-check for shared status-icon table.
 * Run: bun run src/ui/status-icons.selfcheck.ts
 *
 * ponytail: invariant — every canonical subtask status maps to a non-empty
 * icon. Catches drift (e.g. the task-result-renderer `planning` gap that
 * motivated this extraction).
 */
import type { Theme, ThemeColor } from "@oh-my-pi/pi-coding-agent";
import { statusIcon, STATUS_ICON } from "./status-icons";

const theme: Theme = {
	fg: (_c: ThemeColor, text: string) => text,
	bold: (text: string) => text,
} as unknown as Theme;

let failures = 0;
function check(name: string, cond: boolean) {
	console.log(`${cond ? "PASS" : "FAIL"}: ${name}`);
	if (!cond) failures++;
}

// Canonical subtask statuses (from orchestrator.ts SubtaskResult union)
const subtaskStatuses = ["pending", "running", "reviewing", "completed", "failed", "cancelled"];

for (const s of [...subtaskStatuses, "planning"]) {
	const icon = statusIcon(s, theme);
	check(`statusIcon(${s}) non-empty`, icon.length > 0);
}

// Unknown status falls back to pending (non-empty)
check("unknown status falls back to pending", statusIcon("bogus", theme) === statusIcon("pending", theme));

// planning IS in the table (was missing from task-result-renderer pre-extraction)
check("planning in STATUS_ICON table", "planning" in STATUS_ICON);

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
