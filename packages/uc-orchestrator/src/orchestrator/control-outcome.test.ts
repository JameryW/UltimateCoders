/**
 * F26/F27 regression — task-id prefix resolution + discriminated control
 * outcomes (cancel/pause/resume).
 *
 * UIs only ever display truncated ids, so exact-match lookups made every
 * copy-pasted id fail; and the plain boolean return couldn't say why a
 * command failed. These tests drive a real UCOrchestrator (stub bridge,
 * mocked runSubprocess so submits fail fast and leave a task in the map).
 *
 * Run: bun test src/orchestrator/control-outcome.test.ts
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

function makeOrchestrator(): Promise<UCOrchestrator> {
	const workspace = mkdtempSync(join(tmpdir(), "uc-control-outcome-"));
	const pi = {
		pi: { settings: { workspaceRoot: workspace } },
		logger: { warn: () => {}, info: () => {} },
	};
	const bridge = {
		isConnected: () => false,
		setOnConnectionChange: () => {},
		setOnReconnectAttempt: () => {},
		startWatchTask: () => ({ abort: () => {} }),
		upsertTask: () => Promise.resolve(),
		pauseTask: () => Promise.resolve(),
		resumeTask: () => Promise.resolve(),
	} as unknown as GrpcBridge;
	const orch = new UCOrchestrator(pi as never, undefined, bridge);
	return orch.restore().then(() => orch);
}

describe("control command id resolution + outcomes", () => {
	it("resolves exact ids and unique prefixes; rejects ambiguous/not-found with candidates", async () => {
		const orch = await makeOrchestrator();
		const t1 = await orch.submitTask("doomed task one");
		const t2 = await orch.submitTask("doomed task two");

		// exact
		const exact = orch.resolveTask(t1);
		expect("id" in exact && exact.id === t1).toBe(true);
		// unique prefix (displayed ids are 8-14 char slices)
		const byPrefix = orch.resolveTask(t1.slice(0, 8));
		expect("id" in byPrefix && byPrefix.id === t1).toBe(true);
		// ambiguous — both ids share the "uc-" prefix
		const amb = orch.resolveTask("uc-");
		expect("ok" in amb && !amb.ok && amb.reason === "ambiguous").toBe(true);
		if ("ok" in amb && !amb.ok) {
			expect(amb.candidates).toContain(t1);
			expect(amb.candidates).toContain(t2);
		}
		// not found — candidates list recent task ids for guidance
		const nf = orch.resolveTask("zz-nothing");
		expect("ok" in nf && !nf.ok && nf.reason === "not_found").toBe(true);
		if ("ok" in nf && !nf.ok) {
			expect(nf.candidates).toContain(t1);
		}
	});

	it("cancel refuses terminal tasks (bad_state) and names subtask typos", async () => {
		const orch = await makeOrchestrator();
		const tid = await orch.submitTask("doomed task"); // fails → status "failed" (terminal)

		const c1 = await orch.cancelTask(tid);
		expect(c1.ok).toBe(false);
		if (!c1.ok) expect(c1.reason).toBe("bad_state");

		// typo'd subtask id → subtask_not_found (previously surfaced as "task not found")
		const c2 = await orch.cancelTask(tid.slice(0, 8), "st-999");
		expect(c2.ok).toBe(false);
		if (!c2.ok) expect(c2.reason).toBe("subtask_not_found");

		// unknown id → not_found, not a bare false
		const c3 = await orch.cancelTask("zz-nothing");
		expect(c3.ok).toBe(false);
		if (!c3.ok) expect(c3.reason).toBe("not_found");
	});

	it("resume resolves prefixes and pause guards state", async () => {
		const orch = await makeOrchestrator();
		const tid = await orch.submitTask("doomed task"); // failed → resumable

		// resume via prefix — no pending subtasks → completes successfully
		const r1 = await orch.resumeTask(tid.slice(0, 6));
		expect(r1.ok).toBe(true);
		if (r1.ok) expect(r1.taskId).toBe(tid);

		// now completed (terminal): pause and cancel both bad_state
		const p1 = await orch.pauseTask(tid);
		expect(p1.ok).toBe(false);
		if (!p1.ok) expect(p1.reason).toBe("bad_state");
		const c1 = await orch.cancelTask(tid);
		expect(c1.ok).toBe(false);
		if (!c1.ok) expect(c1.reason).toBe("bad_state");
	});
});
