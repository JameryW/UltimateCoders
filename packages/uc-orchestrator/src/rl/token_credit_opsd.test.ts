/**
 * Tests for OPSD4MAS / ORSD token credit (#124).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	TokenCreditError,
	addRubricAdvantage,
	combineTokenAdvantages,
	communicationGain,
	communicationTokenCredit,
	filterCommunicationFacts,
	rubricAdvantage,
} from "./token_credit_opsd";
import type { CommunicationFact, RubricItem } from "./token_credit_opsd";

function fact(overrides: Partial<CommunicationFact> & Pick<CommunicationFact, "id">): CommunicationFact {
	return {
		sender: "planner",
		receiver: "coder",
		tokenIds: [0, 1],
		withFactLogprob: -0.2,
		withoutFactLogprob: -0.5,
		...overrides,
	};
}

describe("OPSD4MAS communication credit", () => {
	it("computes the leave-one-out gain", () => {
		assert.ok(Math.abs(communicationGain(fact({ id: "a" })) - 0.3) < 1e-12);
	});

	it("keeps only positive-gain facts and records the gain", () => {
		const kept = filterCommunicationFacts([
			fact({ id: "useful" }),
			fact({ id: "noise", withFactLogprob: -0.8, withoutFactLogprob: -0.6 }),
		]);
		assert.deepEqual(kept.map((item) => item.id), ["useful"]);
		assert.ok(Math.abs(kept[0].gain - 0.3) < 1e-12);
	});

	it("honours a stricter minimum gain", () => {
		assert.equal(filterCommunicationFacts([fact({ id: "a" })], 0.4).length, 0);
	});

	it("rejects duplicate facts and empty endpoints", () => {
		assert.throws(
			() => filterCommunicationFacts([fact({ id: "a" }), fact({ id: "a" })]),
			TokenCreditError,
		);
		assert.throws(
			() => filterCommunicationFacts([fact({ id: "a", sender: "" })]),
			TokenCreditError,
		);
	});

	it("distributes gain over the produced tokens", () => {
		const credit = communicationTokenCredit(
			[{ ...fact({ id: "a" }), gain: 1.0 }],
			4,
		);
		assert.deepEqual(credit, [0.5, 0.5, 0, 0]);
	});

	it("rejects token ids outside the response", () => {
		assert.throws(
			() => communicationTokenCredit([{ ...fact({ id: "a", tokenIds: [5] }), gain: 1 }], 3),
			TokenCreditError,
		);
	});
});

describe("ORSD dynamic process rubric", () => {
	it("weights only the rubric items with positive weight", () => {
		const items: RubricItem[] = [
			{ id: "test", weight: 1, withRubricLogprob: -0.1, withoutRubricLogprob: -0.4 },
			{ id: "zero", weight: 0, withRubricLogprob: -1, withoutRubricLogprob: -2 },
		];
		const result = rubricAdvantage(items);
		assert.equal(result.activeItems, 1);
		assert.ok(Math.abs(result.advantage - 0.3) < 1e-12);
	});

	it("rejects duplicate or invalid rubric items", () => {
		const item: RubricItem = {
			id: "a",
			weight: 1,
			withRubricLogprob: 0,
			withoutRubricLogprob: -1,
		};
		assert.throws(() => rubricAdvantage([item, item]), TokenCreditError);
		assert.throws(() => rubricAdvantage([{ ...item, weight: -1 }]), TokenCreditError);
		assert.throws(() => rubricAdvantage([{ ...item, withRubricLogprob: Number.NaN }]), TokenCreditError);
	});

	it("adds a rubric advantage uniformly across tokens", () => {
		assert.deepEqual(addRubricAdvantage([0, 0, 0, 0], 1), [0.25, 0.25, 0.25, 0.25]);
		assert.deepEqual(addRubricAdvantage([], 1), []);
	});

	it("combines outcome, dense, and rubric credit", () => {
		const result = combineTokenAdvantages({
			outcomeAdvantage: 1,
			tokenCredit: [0.2, 0.2],
			rubricAdvantage: 0.4,
			denseWeight: 0.5,
			rubricWeight: 0.5,
		});
		assert.ok(Math.abs(result[0] - 1.15) < 1e-12);
		assert.ok(Math.abs(result[1] - 1.15) < 1e-12);
	});

	it("validates weights and finite advantages", () => {
		assert.throws(
			() =>
				combineTokenAdvantages({
					outcomeAdvantage: 0,
					tokenCredit: [],
					rubricAdvantage: 0,
					denseWeight: -1,
				}),
			TokenCreditError,
		);
		assert.throws(
			() => addRubricAdvantage([0], Number.POSITIVE_INFINITY),
			TokenCreditError,
		);
	});
});
