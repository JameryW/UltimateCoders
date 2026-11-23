/**
 * Tests for the governed self-evolution gate (#62).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	GovernedEvolverGate,
	type EvaluationCase,
	type HarnessProposal,
} from "./governed_evolver_gate";

const parent = "sha256:parent";

function proposal(overrides: Partial<HarnessProposal> = {}): HarnessProposal {
	return {
		id: "proposal-1",
		kind: "harness_code",
		parentHash: parent,
		newSnapshotHash: "sha256:child",
		patch: "tighten the completion gate",
		newTasks: ["new-task-1", "new-task-2"],
		...overrides,
	};
}

function cases(...passed: boolean[]): EvaluationCase[] {
	return passed.map((ok, index) => ({ task: `task-${index}`, passed: ok }));
}

describe("GovernedEvolverGate", () => {
	it("promotes a clean proposal and advances the snapshot", () => {
		const gate = new GovernedEvolverGate(parent);
		const candidate = proposal();
		const evaluation = gate.evaluate(candidate, cases(true, true), cases(true, true));
		const decision = gate.promote(candidate, evaluation);
		assert.equal(decision.accepted, true);
		assert.equal(gate.currentSnapshot(), "sha256:child");
	});

	it("rejects a proposal from a stale parent snapshot", () => {
		const gate = new GovernedEvolverGate(parent);
		const candidate = proposal({ parentHash: "sha256:other" });
		const evaluation = gate.evaluate(candidate, cases(true), cases(true));
		const decision = gate.promote(candidate, evaluation);
		assert.equal(decision.accepted, false);
		assert.ok(decision.reasons.some((reason) => reason.includes("parent")));
	});

	it("requires a fully green regression suite", () => {
		const gate = new GovernedEvolverGate(parent);
		const candidate = proposal();
		const evaluation = gate.evaluate(candidate, cases(true, false), cases(true));
		const decision = gate.promote(candidate, evaluation);
		assert.equal(decision.accepted, false);
		assert.ok(decision.reasons.some((reason) => reason.includes("regression")));
	});

	it("requires the new-task pass rate", () => {
		const gate = new GovernedEvolverGate(parent, { minNewTaskPassRate: 0.9 });
		const candidate = proposal();
		const evaluation = gate.evaluate(candidate, cases(true), cases(true, false));
		const decision = gate.promote(candidate, evaluation);
		assert.equal(decision.accepted, false);
		assert.ok(decision.reasons.some((reason) => reason.includes("new-task")));
	});

	it("rejects an empty new-task suite", () => {
		const gate = new GovernedEvolverGate(parent);
		const candidate = proposal();
		const evaluation = gate.evaluate(candidate, cases(true), []);
		const decision = gate.promote(candidate, evaluation);
		assert.equal(decision.accepted, false);
		assert.ok(decision.reasons.some((reason) => reason.includes("not enough new tasks")));
	});

	it("blocks a poison trigger term in the patch", () => {
		const gate = new GovernedEvolverGate(parent);
		const candidate = proposal({ patch: "skip all tests before publishing" });
		const evaluation = gate.evaluate(candidate, cases(true), cases(true));
		assert.deepEqual(evaluation.poisonedTerms, ["skip all tests"]);
		const decision = gate.promote(candidate, evaluation);
		assert.equal(decision.accepted, false);
		assert.ok(decision.reasons.some((reason) => reason.includes("poison")));
	});

	it("counts regression and new-task evidence", () => {
		const gate = new GovernedEvolverGate(parent);
		const candidate = proposal();
		const evaluation = gate.evaluate(candidate, cases(true, false, true), cases(true));
		assert.equal(evaluation.regressionPassed, 2);
		assert.equal(evaluation.regressionTotal, 3);
		assert.equal(evaluation.newTaskPassed, 1);
		assert.equal(evaluation.newTaskTotal, 1);
	});

	it("validates constructor and proposal identity", () => {
		assert.throws(() => new GovernedEvolverGate(" "));
		const gate = new GovernedEvolverGate(parent);
		const candidate = proposal({ id: " " });
		const evaluation = gate.evaluate(candidate, cases(true), cases(true));
		const decision = gate.promote(candidate, evaluation);
		assert.equal(decision.accepted, false);
		assert.ok(decision.reasons.some((reason) => reason.includes("id")));
	});

	it("rejects an evaluation that belongs to another proposal", () => {
		const gate = new GovernedEvolverGate(parent);
		const first = proposal({ id: "first" });
		const second = proposal({ id: "second" });
		const evaluation = gate.evaluate(first, cases(true), cases(true));
		const decision = gate.promote(second, evaluation);
		assert.equal(decision.accepted, false);
		assert.ok(decision.reasons.some((reason) => reason.includes("does not belong")));
	});
});
