/**
 * Tests for SINKFLEX-RL, MAGPIE and PTA-IRT accounting (#54).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	SinkFlexMagpieTrainer,
	SINKFLEX_PUBLISHED_PEAK_REDUCTION,
	magpieAggregate,
	memoryEstimate,
	ptaBenchmarkScore,
	ptaIrtEstimate,
	sigmoid,
	sinkFlexAttention,
} from "./sinkflex_magpie_rl";

describe("SINKFLEX attention identity", () => {
	it("evaluates the zero-value sink identity", () => {
		const result = sinkFlexAttention(0, 4);
		assert.equal(result.alphaSink, 0.5);
		assert.equal(result.output, 2);
		assert.equal(result.sinkLogit, 0);
	});

	it("keeps alpha in [0, 1] and is monotone in the sink logit", () => {
		const low = sinkFlexAttention(-8, 1);
		const high = sinkFlexAttention(8, 1);
		assert.ok(low.alphaSink > 0 && low.alphaSink < 0.001);
		assert.ok(high.alphaSink > 0.999 && high.alphaSink <= 1);
		assert.ok(high.output > low.output);
	});

	it("honours the sink offset", () => {
		const shifted = sinkFlexAttention(4, 2, { sinkEta: 4 });
		assert.equal(shifted.sinkLogit, 0);
		assert.equal(shifted.alphaSink, 0.5);
	});

	it("validates finite inputs", () => {
		assert.throws(() => sinkFlexAttention(Number.NaN, 1), /finite/);
		assert.throws(() => sinkFlexAttention(0, Number.POSITIVE_INFINITY), /finite/);
	});
});

describe("memory accounting", () => {
	it("reproduces the published 4,096-token reduction", () => {
		const estimate = memoryEstimate(28.06, 22.52);
		assert.ok(Math.abs(estimate.reduction - SINKFLEX_PUBLISHED_PEAK_REDUCTION) < 1e-3);
		assert.ok(Math.abs(estimate.savedGb - 5.54) < 1e-9);
	});

	it("rejects invalid memory values", () => {
		assert.throws(() => memoryEstimate(0, 1), /baselineGb/);
		assert.throws(() => memoryEstimate(1, -1), /optimizedGb/);
	});
});

describe("MAGPIE monotone preference aggregation", () => {
	it("normalises weights and computes a weighted value", () => {
		const aggregate = magpieAggregate([
			{ role: "planner", reward: 1, weight: 3 },
			{ role: "coder", reward: 0, weight: 1 },
		]);
		assert.equal(aggregate.value, 0.75);
		assert.equal(aggregate.weights.planner, 0.75);
		assert.equal(aggregate.weights.coder, 0.25);
		assert.deepEqual(aggregate.roles, ["coder", "planner"]);
	});

	it("is monotone in each role reward", () => {
		const baseline = magpieAggregate([
			{ role: "planner", reward: 0.2 },
			{ role: "coder", reward: 0.4 },
		]);
		const improved = magpieAggregate([
			{ role: "planner", reward: 0.5 },
			{ role: "coder", reward: 0.4 },
		]);
		assert.ok(improved.value > baseline.value);
	});

	it("rejects empty, duplicate and non-finite preferences", () => {
		assert.throws(() => magpieAggregate([]), /at least one/);
		assert.throws(
			() =>
				magpieAggregate([
					{ role: "planner", reward: 1 },
					{ role: "planner", reward: 2 },
				]),
			/duplicate role/,
		);
		assert.throws(
			() => magpieAggregate([{ role: "planner", reward: Number.NaN }]),
			/finite/,
		);
		assert.throws(
			() => magpieAggregate([{ role: "planner", reward: 1, weight: 0 }]),
			/weight/,
		);
	});
});

describe("PTA-IRT privileged trace evidence", () => {
	it("raises ability after a passed item and lowers it after a failure", () => {
		const passed = ptaIrtEstimate([{ difficulty: 0, passed: true }]);
		const failed = ptaIrtEstimate([{ difficulty: 0, passed: false }]);
		assert.ok(passed.ability > 0);
		assert.ok(failed.ability < 0);
		assert.equal(passed.evidence, 2);
	});

	it("moves further with a higher trace weight", () => {
		const low = ptaIrtEstimate([{ difficulty: 0, passed: true, traceWeight: 0.2 }]);
		const high = ptaIrtEstimate([{ difficulty: 0, passed: true, traceWeight: 2 }]);
		assert.ok(high.ability > low.ability);
		assert.ok(high.evidence > low.evidence);
	});

	it("returns the prior for an empty trace and bounds the score", () => {
		const estimate = ptaIrtEstimate([], 1.5, 2);
		assert.equal(estimate.ability, 1.5);
		assert.equal(estimate.evidence, 2);
		const score = ptaBenchmarkScore(estimate);
		assert.ok(score > 0 && score < 1);
	});

	it("rejects invalid learning parameters", () => {
		assert.throws(() => ptaIrtEstimate([], 0, -1), /priorWeight/);
		assert.throws(() => ptaIrtEstimate([], 0, 1, 0), /learningRate/);
		assert.throws(
			() => ptaIrtEstimate([{ difficulty: Number.NaN, passed: true }]),
			/difficulty/,
		);
	});

	it("exposes the training facade", () => {
		const trainer = new SinkFlexMagpieTrainer();
		assert.equal(trainer.attention(0, 2).output, 1);
		assert.equal(trainer.aggregate([{ role: "a", reward: 1 }]).value, 1);
		assert.ok(trainer.estimate([{ difficulty: 0, passed: true }]).ability > 0);
	});
});
