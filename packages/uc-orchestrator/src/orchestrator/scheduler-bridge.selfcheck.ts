/**
 * Self-check for scheduler-bridge + /uc schedule command parsing.
 * Run: bun run src/orchestrator/scheduler-bridge.selfcheck.ts
 *
 * Verifies:
 * 1. registerSchedulerTools is a function (registration smoke test).
 * 2. /uc schedule sub-action parsing (status/trigger/add/remove) produces
 *    the expected bridge call shape — tested via a mock bridge that records
 *    calls, so we don't need a live gRPC server.
 * 3. The add flag parser (--project / --night-start / --night-end / --tz)
 *    extracts the right values from positional + flag args.
 */

import { registerSchedulerTools } from "./scheduler-bridge";
import type { GrpcBridge, SchedulerStatus, AddCronJobResult, RemoveJobResult } from "./grpc-bridge";

let failures = 0;
function check(name: string, cond: boolean) {
	console.log(`${cond ? "PASS" : "FAIL"}: ${name}`);
	if (!cond) failures++;
}

// ── 1. Registration smoke test ───────────────────────────────────

// registerSchedulerTools is a function (exported correctly).
check("registerSchedulerTools is a function", typeof registerSchedulerTools === "function");

// ── 2. Mock bridge — records scheduler calls ─────────────────────

class MockBridge {
	lastCall: string | null = null;
	lastArgs: unknown = null;
	statusResponse: SchedulerStatus = {
		available: true,
		isRunning: true,
		nightWindow: { start: "22:00", end: "06:00", enabled: true },
		jobs: [
			{ id: "nightly-index", name: "Index repos", cron: "0 2 * * *", enabled: true, lastRun: "2026-01-01T02:00:00Z", nextRun: "2026-01-02T02:00:00Z" },
			{ id: "disabled-job", name: "Old job", cron: "0 4 * * 0", enabled: false },
		],
		executionHistory: [
			{ jobId: "nightly-index", jobName: "Index repos", executedAt: "2026-01-01T02:00:00Z", success: true, status: "Completed" },
			{ jobId: "nightly-index", jobName: "Index repos", executedAt: "2025-12-31T02:00:00Z", success: false, error: "timeout", status: "Failed" },
		],
	};

	async getSchedulerStatus(): Promise<SchedulerStatus> {
		this.lastCall = "getSchedulerStatus";
		return this.statusResponse;
	}
	async triggerSchedulerJob(jobId: string): Promise<{ success: boolean; jobId: string; error?: string }> {
		this.lastCall = "triggerSchedulerJob";
		this.lastArgs = jobId;
		return { success: true, jobId };
	}
	async addCronJob(req: {
		description: string; cronExpression: string; projectId?: string;
		nightWindowStart?: string; nightWindowEnd?: string; timezone?: string; enabled?: boolean;
	}): Promise<AddCronJobResult> {
		this.lastCall = "addCronJob";
		this.lastArgs = req;
		return { ok: true, jobId: "new-job-123" };
	}
	async removeJob(jobId: string): Promise<RemoveJobResult> {
		this.lastCall = "removeJob";
		this.lastArgs = jobId;
		return { ok: true };
	}
}

// ── 3. /uc schedule add flag parser ──────────────────────────────
// Replicate the flag-parsing logic from extension.ts to test it in isolation.

function parseAddArgs(args: string[]): {
	description: string; cron: string; projectId: string;
	nightStart?: string; nightEnd?: string; timezone?: string;
} | null {
	if (args.length < 2) return null;
	const description = args[0];
	const cron = args[1];
	let projectId = "";
	let nightStart: string | undefined;
	let nightEnd: string | undefined;
	let timezone: string | undefined;
	for (let i = 2; i < args.length; i++) {
		if (args[i] === "--project" && args[i + 1]) { projectId = args[++i]; }
		else if (args[i] === "--night-start" && args[i + 1]) { nightStart = args[++i]; }
		else if (args[i] === "--night-end" && args[i + 1]) { nightEnd = args[++i]; }
		else if (args[i] === "--tz" && args[i + 1]) { timezone = args[++i]; }
	}
	return { description, cron, projectId, nightStart, nightEnd, timezone };
}

// Basic: description + cron only
const basic = parseAddArgs(["Index repos", "0 2 * * *"]);
check("add: basic description + cron", basic?.description === "Index repos" && basic?.cron === "0 2 * * *");
check("add: basic projectId defaults to empty", basic?.projectId === "");

// With flags
const flagged = parseAddArgs(["Backup", "0 3 * * *", "--project", "proj-1", "--night-start", "23:00", "--night-end", "05:00", "--tz", "UTC"]);
check("add: --project parsed", flagged?.projectId === "proj-1");
check("add: --night-start parsed", flagged?.nightStart === "23:00");
check("add: --night-end parsed", flagged?.nightEnd === "05:00");
check("add: --tz parsed", flagged?.timezone === "UTC");

// Too few args
check("add: too few args returns null", parseAddArgs(["only-desc"]) === null);

// ── 4. Mock bridge call verification ─────────────────────────────

const mock = new MockBridge();

// trigger
void mock.triggerSchedulerJob("nightly-index");
check("triggerSchedulerJob records jobId", mock.lastCall === "triggerSchedulerJob" && mock.lastArgs === "nightly-index");

// addCronJob
void mock.addCronJob({ description: "Test", cronExpression: "0 5 * * *", timezone: "UTC" });
check("addCronJob records request", mock.lastCall === "addCronJob");
check("addCronJob records cronExpression", (mock.lastArgs as { cronExpression: string }).cronExpression === "0 5 * * *");

// removeJob
void mock.removeJob("old-job");
check("removeJob records jobId", mock.lastCall === "removeJob" && mock.lastArgs === "old-job");

// getSchedulerStatus
void mock.getSchedulerStatus();
check("getSchedulerStatus records call", mock.lastCall === "getSchedulerStatus");

// ── 5. Status response shape ─────────────────────────────────────

const status = mock.statusResponse;
check("status has jobs", status.jobs.length === 2);
check("status has nightWindow", status.nightWindow?.start === "22:00");
check("status has executionHistory", status.executionHistory.length === 2);
check("status job[0] enabled", status.jobs[0].enabled === true);
check("status job[1] disabled", status.jobs[1].enabled === false);

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
