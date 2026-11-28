/**
 * Tests for RiseHarness flipping-task diagnosis (#61).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	HarnessHandbook,
	RiseHarnessEvolver,
	alignTraces,
	findFlippingTasks,
	type FlippingTask,
	type HarnessAttempt,
} from "./rise_harness_evolver";

function attempt(
	id: string,
	success: boolean,
	configHash = "cfg-a",
	agentEvent = "run tests",
	harnessEvent = "ok",
): HarnessAttempt {
	return {
		attemptId: id,
		model: "model-a",
		configHash,
		success,
		agentTrace: [{ step: 1, agentAction: agentEvent }],
		harnessTrace: [{ step: 1, harnessEvent }],
	};
}

function task(id: string, attempts: readonly HarnessAttempt[]): FlippingTask {
	return { taskId: id, attempts };
}

describe("HarnessHandbook", () => {
	it("registers, locates, and renders responsibilities", () => {
		const handbook = new HarnessHandbook();
		handbook.register({
			responsibility: "feedback_truncation",
			sourceFile: "python/ultimate_coders/agent/harness_local_loop.py",
			functionName: "truncate_observation",
			notes: "keep the failing assertion",
		});
		assert.equal(handbook.locate("feedback_truncation")?.functionName, "truncate_observation");
		assert.equal(handbook.all().length, 1);
		assert.match(handbook.render(), /feedback_truncation/);
	});

	it("rejects duplicate responsibility and empty source", () => {
		const handbook = new HarnessHandbook();
		handbook.register({
			responsibility: "completion_gate",
			sourceFile: "gate.py",
			functionName: "finish",
			notes: "",
		});
		assert.throws(() =>
			handbook.register({
				responsibility: "completion_gate",
				sourceFile: "other.py",
				functionName: "finish",
				notes: "",
			}),
		);
		assert.throws(() =>
			handbook.register({
				responsibility: "command_execution",
				sourceFile: "",
				functionName: "run",
				notes: "",
			}),
		);
	});
});

describe("flipping tasks", () => {
	it("keeps tasks with both outcomes under one model/config", () => {
		const tasks = [
			task("flip", [attempt("a", true), attempt("b", false)]),
			task("always-pass", [attempt("c", true)]),
			task("two-configs", [
				attempt("d", true, "cfg-a"),
				attempt("e", false, "cfg-b"),
			]),
		];
		assert.deepEqual(
			findFlippingTasks(tasks).map((entry) => entry.taskId),
			["flip"],
		);
	});

	it("returns empty when no task flips", () => {
		assert.deepEqual(findFlippingTasks([task("pass", [attempt("a", true)])]), []);
	});
});

describe("trace alignment and diagnosis", () => {
	it("marks missing or suspicious harness events", () => {
		const aligned = alignTraces(
			[
				{ step: 1, agentAction: "run tests" },
				{ step: 2, agentAction: "read file" },
				{ step: 3, agentAction: "edit file" },
			],
			[
				{ step: 1, harnessEvent: "ok" },
				{ step: 3, harnessEvent: "truncated" },
			],
		);
		assert.equal(aligned[1].suspicious, true);
		assert.equal(aligned[2].suspicious, true);
		assert.equal(aligned[0].suspicious, false);
	});

	it("emits only responsibilities present in the handbook", () => {
		const handbook = new HarnessHandbook();
		handbook.register({
			responsibility: "feedback_truncation",
			sourceFile: "feedback.py",
			functionName: "truncate_observation",
			notes: "",
		});
		handbook.register({
			responsibility: "completion_gate",
			sourceFile: "gate.py",
			functionName: "finish",
			notes: "",
		});
		const evolver = new RiseHarnessEvolver(handbook);
		const report = evolver.diagnose([
			task("flip", [
				attempt("ok", true, "cfg-a"),
				attempt("bad", false, "cfg-a", "run tests", "truncated"),
			]),
		]);
		assert.equal(report.proposals.length, 1);
		assert.equal(report.proposals[0].responsibility, "feedback_truncation");
		assert.match(report.proposals[0].evidence, /flip\/bad/);
		assert.ok(report.alignments.has("bad"));
	});

	it("does not diagnose successful attempts", () => {
		const handbook = new HarnessHandbook();
		handbook.register({
			responsibility: "completion_gate",
			sourceFile: "gate.py",
			functionName: "finish",
			notes: "",
		});
		const report = new RiseHarnessEvolver(handbook).diagnose([
			task("flip", [
				attempt("ok", true, "cfg-a", "done", "false_completion"),
				attempt("also-ok", true, "cfg-a"),
			]),
		]);
		assert.equal(report.flippingTasks.length, 0);
		assert.deepEqual(report.proposals, []);
	});

	it("requires a handbook entry and an external framework for a port", () => {
		const handbook = new HarnessHandbook();
		handbook.register({
			responsibility: "command_execution",
			sourceFile: "run.py",
			functionName: "execute",
			notes: "",
		});
		const evolver = new RiseHarnessEvolver(handbook);
		const proposal = evolver.port("command_execution", "external-harness");
		assert.match(proposal.evidence, /external-harness/);
		assert.throws(() => evolver.port("completion_gate", "external"));
		assert.throws(() => evolver.port("command_execution", " "));
	});
});
