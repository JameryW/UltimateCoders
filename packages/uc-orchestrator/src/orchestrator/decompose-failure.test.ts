/**
 * F13 regression — decomposition failure must emit task_complete.
 *
 * Before the fix the decompose catch block marked the task failed and
 * returned without emitting task_complete, stranding task_planning's
 * "UC: Planning..." working message, "UC: planning" footer field, and
 * progressState entry forever (only the extension's task_complete handler
 * cleans them up).
 *
 * runSubprocess (the local decomposition path) is mocked to throw; the stub
 * bridge reports disconnected so the remote path is skipped entirely.
 *
 * Run: bun test src/orchestrator/decompose-failure.test.ts
 */

import { describe, expect, it, mock } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

mock.module("@oh-my-pi/pi-coding-agent", () => ({
	runSubprocess: async () => {
		throw new Error("decompose boom");
	},
}));

import { UCOrchestrator } from "./orchestrator";
import type { GrpcBridge } from "./grpc-bridge";

describe("submitTask decomposition failure", () => {
	it("emits task_complete with status failed (no stranded planning state)", async () => {
		const workspace = mkdtempSync(join(tmpdir(), "uc-decompose-fail-"));
		const pi = {
			pi: { settings: { workspaceRoot: workspace } },
			logger: { warn: () => {}, info: () => {} },
		};
		// Disconnected bridge → checkWorkerAvailability() false → local decompose
		// path → mocked runSubprocess throws → catch block under test.
		const bridge = {
			isConnected: () => false,
			setOnConnectionChange: () => {},
			setOnReconnectAttempt: () => {},
			startWatchTask: () => ({ abort: () => {} }),
			upsertTask: () => Promise.resolve(),
		} as unknown as GrpcBridge;

		const orch = new UCOrchestrator(pi as never, undefined, bridge);
		await orch.restore(); // init TaskStore; controlSubscriber start fails harmlessly on the stub
		const completed: Array<{ taskId: string; status: string }> = [];
		orch.events.on("task_complete", (d) => completed.push({ taskId: d.taskId, status: d.status }));

		const taskId = await orch.submitTask("doomed task");

		expect(completed.length).toBe(1);
		expect(completed[0].taskId).toBe(taskId);
		expect(completed[0].status).toBe("failed");
		expect(orch.getTaskState(taskId)?.error).toContain("decompose boom");
	});
});
