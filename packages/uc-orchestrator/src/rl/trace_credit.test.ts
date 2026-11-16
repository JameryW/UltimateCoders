/**
 * Tests for TRACE turn-level credit assignment (#20).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	ALPHA_OUT,
	ALPHA_TURN,
	applyTraceCredit,
	computeTurnCredits,
	GAMMA,
	K,
	LAMBDA_TERM,
	REWARD_BASELINE,
} from "./trace_credit";

const EPSILON = 1e-12;

function distance(likelihood: number): number {
	return -Math.log(likelihood) + REWARD_BASELINE;
}

function expectedDelta(likelihoods: readonly number[], turn: number): number {
	return Math.log(distance(likelihoods[turn]) / distance(likelihoods[turn + 1]));
}

function expectedCredit(likelihoods: readonly number[], turn: number, horizon = K, gamma = GAMMA): number {
	if (horizon === 0) return 0;
	const last = Math.min(turn + horizon - 1, likelihoods.length - 2);
	let weighted = 0;
	let normalizer = 0;
	for (let u = turn; u <= last; u++) {
		const weight = gamma ** (u - turn);
		weighted += weight * expectedDelta(likelihoods, u);
		normalizer += weight;
	}
	return normalizer === 0 ? 0 : weighted / normalizer;
}

function assertClose(actual: number, expected: number, message?: string): void {
	assert.ok(Math.abs(actual - expected) <= EPSILON, `${message ?? "value"}: expected ${expected}, got ${actual}`);
}

describe("TRACE constants", () => {
	it("matches the published hyperparameters", () => {
		assert.equal(K, 3);
		assert.equal(GAMMA, 0.8);
		assert.equal(LAMBDA_TERM, 2.0);
		assert.equal(ALPHA_OUT, 1.0);
		assert.equal(ALPHA_TURN, 0.2);
		assert.equal(REWARD_BASELINE, 0.1);
	});
});

describe("computeTurnCredits", () => {
	it("computes one credit per transition from T+1 answer likelihoods", () => {
		const likelihoods = [0.5, 0.25, 0.125, 0.0625];

		const credits = computeTurnCredits(likelihoods);

		assert.equal(credits.length, likelihoods.length - 1);
		for (let turn = 0; turn < credits.length; turn++) {
			assert.equal(credits[turn].turn, turn);
			assert.equal(credits[turn].logRatio, Math.log(distance(likelihoods[0]) / distance(likelihoods[turn])));
			assert.equal(credits[turn].delta, expectedDelta(likelihoods, turn));
		}
	});

	it("uses the truncated K=3 TD average", () => {
		const likelihoods = [0.5, 0.25, 0.125, 0.0625];

		const credits = computeTurnCredits(likelihoods);

		for (let turn = 0; turn < credits.length; turn++) {
			assertClose(credits[turn].credit, expectedCredit(likelihoods, turn, 3, 0.8), `credit[${turn}]`);
		}
	});

	it("applies the gamma discount inside the truncation window", () => {
		const likelihoods = [0.9, 0.8, 0.7, 0.6, 0.5];
		const gamma = 0.5;

		const credits = computeTurnCredits(likelihoods, { K: 2, gamma });

		for (let turn = 0; turn < credits.length; turn++) {
			assertClose(credits[turn].credit, expectedCredit(likelihoods, turn, 2, gamma), `credit[${turn}]`);
		}
		// First window: delta_0 + 0.5 * delta_1 over 1.5.
		assertClose(credits[0].credit, (expectedDelta(likelihoods, 0) + 0.5 * expectedDelta(likelihoods, 1)) / 1.5);
	});

	it("K=1 uses raw deltas and K=0 disables dense credit", () => {
		const likelihoods = [0.9, 0.8, 0.7, 0.6];

		const oneStep = computeTurnCredits(likelihoods, { K: 1 });
		assert.equal(oneStep.length, likelihoods.length - 1);
		for (let turn = 0; turn < oneStep.length; turn++) {
			assert.equal(oneStep[turn].credit, expectedDelta(likelihoods, turn));
		}

		const noTd = computeTurnCredits(likelihoods, { K: 0 });
		assert.equal(noTd.length, likelihoods.length - 1);
		assert.ok(noTd.every((entry) => entry.credit === 0));
		assert.equal(noTd[0].logRatio, 0);
	});

	it("returns an empty array for empty or single-state input", () => {
		assert.deepEqual(computeTurnCredits([]), []);
		assert.deepEqual(computeTurnCredits([0.5]), []);
	});

	it("rejects invalid likelihoods instead of producing NaN", () => {
		for (const invalid of [0, -0.1, 1.1, Number.NaN, Number.POSITIVE_INFINITY]) {
			assert.throws(() => computeTurnCredits([0.5, invalid]), RangeError);
		}
		assert.throws(() => computeTurnCredits([0]), RangeError);
	});

	it("rejects invalid options", () => {
		const likelihoods = [0.5, 0.25];
		assert.throws(() => computeTurnCredits(likelihoods, { K: -1 }), RangeError);
		assert.throws(() => computeTurnCredits(likelihoods, { K: 1.5 }), RangeError);
		assert.throws(() => computeTurnCredits(likelihoods, { gamma: 1.1 }), RangeError);
		assert.throws(() => computeTurnCredits(likelihoods, { gamma: -0.1 }), RangeError);
		assert.throws(() => computeTurnCredits(likelihoods, { baseline: 0 }), RangeError);
	});
});

describe("applyTraceCredit", () => {
	it("mixes outcome advantage with dense credit and the terminal reward", () => {
		const likelihoods = [0.5, 0.25, 0.125];
		const outcomeAdvantage = 1;
		const credits = computeTurnCredits(likelihoods);
		const totalTurns = credits.length;

		const advantages = applyTraceCredit(likelihoods, outcomeAdvantage);

		assert.equal(advantages.length, totalTurns);
		for (let turn = 0; turn < totalTurns; turn++) {
			// Paper Eq. 10: gamma^(T-k), with final-answer generation as one extra transition.
			const terminal = LAMBDA_TERM * GAMMA ** (totalTurns - turn) * outcomeAdvantage;
			const expected = ALPHA_OUT * outcomeAdvantage + ALPHA_TURN * (credits[turn].credit + terminal);
			assertClose(advantages[turn], expected, `advantage[${turn}]`);
		}
		// Both windows reach the end for a 2-turn trajectory; the final turn is discounted once.
		assertClose(advantages[1], ALPHA_OUT * outcomeAdvantage + ALPHA_TURN * (credits[1].credit + 2 * GAMMA));
	});

	it("applies the paper's gamma^(T-k) terminal exponent across a longer trajectory", () => {
		const likelihoods = [0.9, 0.8, 0.7, 0.6, 0.5];
		const credits = computeTurnCredits(likelihoods);
		const totalTurns = credits.length;

		const advantages = applyTraceCredit(credits, 1);

		// K=3 windows: turn 0 ends at 2 (no terminal), turns 1..3 reach the final turn.
		assertClose(advantages[0], ALPHA_OUT + ALPHA_TURN * credits[0].credit, "advantage[0]");
		for (const turn of [1, 2, 3]) {
			assertClose(
				advantages[turn],
				ALPHA_OUT + ALPHA_TURN * (credits[turn].credit + LAMBDA_TERM * GAMMA ** (totalTurns - turn)),
				`advantage[${turn}]`,
			);
		}
	});

	it("accepts precomputed TurnCredit records", () => {
		const likelihoods = [0.5, 0.25, 0.125, 0.0625];
		const credits = computeTurnCredits(likelihoods);

		const fromLikelihoods = applyTraceCredit(likelihoods, 0.5);
		const fromCredits = applyTraceCredit(credits, 0.5);

		assert.deepEqual(fromCredits, fromLikelihoods);
	});

	it("adds no terminal reward when the truncation window does not reach the end", () => {
		const likelihoods = [0.9, 0.8, 0.7, 0.6, 0.5];
		const credits = computeTurnCredits(likelihoods, { K: 1 });

		const advantages = applyTraceCredit(credits, 1, { K: 1 });

		for (let turn = 0; turn < credits.length - 1; turn++) {
			assertClose(advantages[turn], ALPHA_OUT + ALPHA_TURN * credits[turn].credit, `advantage[${turn}]`);
		}
		assertClose(
			advantages[credits.length - 1],
			ALPHA_OUT + ALPHA_TURN * (credits[credits.length - 1].credit + LAMBDA_TERM * GAMMA),
		);
	});

	it("uses zero terminal reward with K=0 except at the final turn", () => {
		const likelihoods = [0.9, 0.8, 0.7, 0.6];
		const credits = computeTurnCredits(likelihoods, { K: 0 });

		const advantages = applyTraceCredit(credits, 2, { K: 0 });

		for (let turn = 0; turn < credits.length - 1; turn++) {
			assert.equal(advantages[turn], ALPHA_OUT * 2);
		}
		assertClose(advantages[credits.length - 1], ALPHA_OUT * 2 + ALPHA_TURN * LAMBDA_TERM * 2 * GAMMA);
	});

	it("returns an empty array for empty input", () => {
		assert.deepEqual(applyTraceCredit([], 1), []);
		assert.deepEqual(applyTraceCredit([], 1, { K: 0 }), []);
	});

	it("rejects a non-finite outcome advantage", () => {
		assert.throws(() => applyTraceCredit([0.5, 0.25], Number.NaN), RangeError);
		assert.throws(() => applyTraceCredit([], Number.POSITIVE_INFINITY), RangeError);
	});
});
