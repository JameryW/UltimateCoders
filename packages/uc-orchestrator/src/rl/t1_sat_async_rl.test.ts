/**
 * Tests for T1 token-exact batches and SAT trust regions (#64).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	StalenessAdaptiveTrustRegion,
	buildTitoBatch,
	quantile,
	replayMoERoutes,
	stalenessFromLogRatio,
	t1DenseReward,
	type SampledToken,
} from "./t1_sat_async_rl";

function token(
	id: number,
	turn: number,
	logprob = -0.1,
	route = "expert-a",
): SampledToken {
	return { id, turn, logprob, route };
}

describe("T1 token-exact batches", () => {
	it("preserves sampled token ids and turn ids", () => {
		const batch = buildTitoBatch([token(10, 0), token(11, 1), token(12, 1)]);
		assert.deepEqual(batch.ids, [10, 11, 12]);
		assert.deepEqual(batch.turnIds, [0, 1, 1]);
		assert.deepEqual(batch.lossMask, [true, true, true]);
		assert.equal(batch.driftRepairs, 0);
	});

	it("repairs a decreasing turn boundary in place", () => {
		const batch = buildTitoBatch([token(1, 0), token(2, 2), token(3, 1)]);
		assert.deepEqual(batch.turnIds, [0, 2, 2]);
		assert.equal(batch.driftRepairs, 1);
	});

	it("honours a supplied loss mask", () => {
		const batch = buildTitoBatch([token(1, 0), token(2, 0)], [true, false]);
		assert.deepEqual(batch.lossMask, [true, false]);
	});

	it("rejects empty, invalid, and misaligned inputs", () => {
		assert.throws(() => buildTitoBatch([]));
		assert.throws(() => buildTitoBatch([token(-1, 0)]));
		assert.throws(() => buildTitoBatch([token(1, -1)]));
		assert.throws(() => buildTitoBatch([token(1, 0)], [true, false]));
	});

	it("replays the first MoE route observed in each turn", () => {
		const replay = replayMoERoutes([
			token(1, 0, -0.1, "expert-a"),
			token(2, 0, -0.2, "expert-b"),
			token(3, 1, -0.1, "expert-c"),
		]);
		assert.deepEqual(replay.routes, ["expert-a", "expert-a", "expert-c"]);
		assert.equal(replay.replayed, 1);
	});

	it("uses the default route when routing is absent", () => {
		const replay = replayMoERoutes([{ id: 1, turn: 0, logprob: -0.1 }]);
		assert.deepEqual(replay.routes, ["default"]);
	});
});

describe("SAT staleness-adaptive trust region", () => {
	it("computes quantiles and absolute staleness", () => {
		assert.equal(quantile([3, 1, 2], 0), 1);
		assert.equal(quantile([3, 1, 2], 0.5), 2);
		assert.equal(quantile([3, 1, 2], 1), 3);
		assert.equal(stalenessFromLogRatio(-0.7), 0.7);
		assert.throws(() => quantile([], 0.5));
		assert.throws(() => quantile([1], 2));
		assert.throws(() => stalenessFromLogRatio(Number.NaN));
	});

	it("shrinks only the stale tail outward bound", () => {
		const region = new StalenessAdaptiveTrustRegion(0.2, 0.9, 0.5);
		const range = region.clipRange([0.01, 0.02, 2.0]);
		assert.equal(range.threshold, 2.0);
		assert.equal(region.clipBounds(0.01, range.threshold).positive, 0.2);
		assert.equal(region.clipBounds(2.0, range.threshold).positive, 0.1);
		assert.equal(region.clipBounds(2.0, range.threshold).negative, -0.1);
	});

	it("clips stale and fresh ratios differently", () => {
		const region = new StalenessAdaptiveTrustRegion(0.2, 0.9, 0.5);
		assert.equal(region.objective(1, 2, 0.01, 1), 0.2);
		assert.equal(region.objective(1, 2, 2, 1), 0.1);
		assert.equal(region.objective(1, -2, 2, 1), -0.1);
	});

	it("validates constructor and objective inputs", () => {
		assert.throws(() => new StalenessAdaptiveTrustRegion(0));
		assert.throws(() => new StalenessAdaptiveTrustRegion(1));
		assert.throws(() => new StalenessAdaptiveTrustRegion(0.2, 0));
		assert.throws(() => new StalenessAdaptiveTrustRegion(0.2, 0.9, 1));
		const region = new StalenessAdaptiveTrustRegion();
		assert.throws(() => region.objective(Number.NaN, 1, 0, 1));
	});
});

describe("T1 dense sub-verifier reward", () => {
	it("adds a bounded bonus over the pass fraction", () => {
		assert.equal(t1DenseReward({ passed: 0, total: 2 }), 0);
		assert.equal(t1DenseReward({ passed: 1, total: 2 }), 0.6);
		assert.equal(t1DenseReward({ passed: 2, total: 2 }), 1.2);
	});

	it("rejects a zero-total sub-verifier", () => {
		assert.throws(() => t1DenseReward({ passed: 0, total: 0 }));
	});
});
