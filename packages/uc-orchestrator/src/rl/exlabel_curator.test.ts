/**
 * Tests for ExLabel task-clarity and test-coverage curation (#41).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	ExLabelCurator,
	type ExperienceRecord,
	type TestCoverageInput,
} from "./exlabel_curator";

function record(overrides: Partial<ExperienceRecord> = {}): ExperienceRecord {
	return {
		id: "r1",
		ambiguityLabels: ["vague acceptance criteria"],
		clarity: 0.2,
		f2pCoverage: 0.5,
		p2pCoverage: 0.5,
		...overrides,
	};
}

function tests(overrides: Partial<TestCoverageInput> = {}): TestCoverageInput {
	return { f2pTotal: 10, f2pCovered: 10, p2pTotal: 5, p2pCovered: 5, ...overrides };
}

describe("ExLabelCurator", () => {
	it("learns label severity from low-clarity adjudications", () => {
		const curator = new ExLabelCurator();
		curator.learn(record({ clarity: 0.2 }));
		curator.learn(record({ id: "r2", clarity: 0.8 }));
		assert.equal(curator.bankSize, 2);
		assert.equal(curator.labelWeight("Vague Acceptance Criteria"), 1.0);
		assert.deepEqual(curator.labels(), ["vague acceptance criteria"]);
	});

	it("penalises learned labels and missing source-file mentions", () => {
		const curator = new ExLabelCurator();
		curator.learn(record());
		const vague = curator.evaluateClarity("vague acceptance criteria for cache");
		const grounded = curator.evaluateClarity(
			"vague acceptance criteria in src/cache.py",
		);
		assert.equal(vague.sourceFileMention, false);
		assert.equal(grounded.sourceFileMention, true);
		assert.equal(vague.score, 0.25);
		assert.equal(grounded.score, 0.4);
	});

	it("caps the total label penalty", () => {
		const curator = new ExLabelCurator({ labelPenaltyCap: 0.6 });
		curator.learn(record({ clarity: 0 }));
		curator.learn(record({ id: "r2", ambiguityLabels: ["underspecified error handling"], clarity: 0 }));
		const score = curator.evaluateClarity(
			"vague acceptance criteria and underspecified error handling in src/cache.py",
		);
		assert.equal(score.score, 0.4);
	});

	it("computes weighted F2P/P2P coverage and missing counts", () => {
		const curator = new ExLabelCurator();
		const coverage = curator.evaluateCoverage(
			tests({ f2pTotal: 10, f2pCovered: 9, p2pTotal: 4, p2pCovered: 3 }),
		);
		assert.equal(coverage.f2pCoverage, 0.9);
		assert.equal(coverage.p2pCoverage, 0.75);
		assert.equal(coverage.score, 0.855);
		assert.equal(coverage.missingF2P, 1);
		assert.equal(coverage.missingP2P, 1);
	});

	it("does not penalise tasks with no P2P tests", () => {
		const curator = new ExLabelCurator();
		const coverage = curator.evaluateCoverage(
			tests({ f2pTotal: 4, f2pCovered: 4, p2pTotal: 0, p2pCovered: 0 }),
		);
		assert.equal(coverage.p2pCoverage, 1);
		assert.equal(coverage.score, 1);
	});

	it("keeps only well-specified and well-tested trajectories", () => {
		const curator = new ExLabelCurator();
		const verdict = curator.curate("Change cache behavior in src/cache.py", tests());
		assert.equal(verdict.keep, true);
		assert.deepEqual(verdict.reasons, []);
	});

	it("drops low-clarity tasks", () => {
		const curator = new ExLabelCurator();
		curator.learn(record());
		const verdict = curator.curate("vague acceptance criteria for cache", tests());
		assert.equal(verdict.keep, false);
		assert.ok(verdict.reasons.includes("low_task_clarity"));
	});

	it("drops tasks without F2P tests", () => {
		const curator = new ExLabelCurator();
		const verdict = curator.curate(
			"Change cache behavior in src/cache.py",
			tests({ f2pTotal: 0, f2pCovered: 0 }),
		);
		assert.equal(verdict.keep, false);
		assert.ok(verdict.reasons.includes("missing_f2p_tests"));
	});

	it("drops partial F2P and P2P coverage", () => {
		const curator = new ExLabelCurator();
		const verdict = curator.curate(
			"Change cache behavior in src/cache.py",
			tests({ f2pTotal: 10, f2pCovered: 2, p2pTotal: 10, p2pCovered: 0 }),
		);
		assert.equal(verdict.keep, false);
		assert.ok(verdict.reasons.includes("insufficient_f2p_coverage"));
		assert.ok(verdict.reasons.includes("insufficient_p2p_coverage"));
	});

	it("filters a batch without reordering", () => {
		const curator = new ExLabelCurator();
		const verdicts = curator.curateBatch([
			{ task: "Change cache in src/cache.py", tests: tests() },
			{ task: "Change cache", tests: tests({ f2pTotal: 0, f2pCovered: 0 }) },
		]);
		assert.deepEqual(verdicts.map((verdict) => verdict.keep), [true, false]);
	});

	it("rejects invalid inputs", () => {
		assert.throws(() => new ExLabelCurator({ clarityThreshold: 1.5 }), RangeError);
		assert.throws(() => new ExLabelCurator({ labelPenaltyCap: -0.1 }), RangeError);
		const curator = new ExLabelCurator();
		assert.throws(() => curator.learn(record({ id: " " })), TypeError);
		assert.throws(() => curator.learn(record({ ambiguityLabels: [] })), RangeError);
		assert.throws(() => curator.learn(record({ clarity: Number.NaN })), RangeError);
		assert.throws(() => curator.evaluateClarity(" "), TypeError);
		assert.throws(
			() => curator.evaluateCoverage(tests({ f2pCovered: 11 })),
			RangeError,
		);
		assert.throws(
			() => curator.evaluateCoverage(tests({ p2pTotal: 1.5 })),
			RangeError,
		);
	});

	it("reports a bounded diagnostic snapshot", () => {
		const curator = new ExLabelCurator();
		curator.learn(record());
		assert.deepEqual(curator.toJSON(), {
			bankSize: 1,
			labels: ["vague acceptance criteria"],
			thresholds: { clarity: 0.6, coverage: 0.6 },
		});
	});
});
