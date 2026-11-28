/**
 * Tests for RST solution-first quadruple synthesis (#68).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	RSTQuadrupleSynthesizer,
	RstSynthesisError,
	advanceQuadruple,
	proveQuadruple,
	type Quadruple,
} from "./rst_synthesizer";

function seed(): Quadruple {
	return {
		workspace: "workspace-v0",
		solution: "reference-v0",
		verifier: "verify.sh",
		instruction: "repair the service",
		basePass: false,
		solutionPass: true,
		commandCount: 2,
		stepCount: 1,
	};
}

describe("RST quadruple synthesis", () => {
	it("accepts a solvable non-trivial seed", () => {
		assert.deepEqual(proveQuadruple(seed()), {
			solvable: true,
			nontrivial: true,
			accepted: true,
		});
	});

	it("rejects trivial or unsolvable seeds", () => {
		assert.throws(
			() => new RSTQuadrupleSynthesizer({ ...seed(), basePass: true }),
			RstSynthesisError,
		);
		assert.throws(
			() => new RSTQuadrupleSynthesizer({ ...seed(), solutionPass: false }),
			RstSynthesisError,
		);
	});

	it("advance extends the solution before the instruction and keeps the proof", () => {
		const next = advanceQuadruple(seed(), 1);
		assert.ok(next.solution.includes("solution extension 1"));
		assert.ok(next.instruction.includes("requirement 1"));
		assert.equal(next.basePass, false);
		assert.equal(next.solutionPass, true);
		assert.equal(proveQuadruple(next).accepted, true);
	});

	it("step returns a numbered accepted task", () => {
		const synthesizer = new RSTQuadrupleSynthesizer(seed());
		const step = synthesizer.step();
		assert.equal(step.round, 1);
		assert.equal(step.proof.accepted, true);
		assert.equal(step.task.commandCount, 3);
	});

	it("generate produces fifteen rounds with growth", () => {
		const synthesizer = new RSTQuadrupleSynthesizer(seed());
		const report = synthesizer.generate(15);
		assert.equal(report.total, 15);
		assert.equal(report.accepted, 15);
		assert.ok(report.solutionGrowth > 1);
		assert.ok(report.commandGrowth > 1);
	});

	it("estimated cost follows the published per-task unit", () => {
		const report = new RSTQuadrupleSynthesizer(seed(), {
			maxRounds: 4,
			costPerTaskUsd: 0.05,
		}).generate(4);
		assert.ok(Math.abs(report.estimatedCostUsd - 0.2) < 1e-12);
	});

	it("enforces the maximum recursion depth", () => {
		const synthesizer = new RSTQuadrupleSynthesizer(seed(), { maxRounds: 2 });
		synthesizer.generate(2);
		assert.throws(() => synthesizer.step(), RstSynthesisError);
	});

	it("reset clears generated steps", () => {
		const synthesizer = new RSTQuadrupleSynthesizer(seed());
		synthesizer.generate(3);
		synthesizer.reset();
		assert.equal(synthesizer.generate(3).total, 3);
	});
});
