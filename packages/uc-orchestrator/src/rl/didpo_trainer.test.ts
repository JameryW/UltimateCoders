/**
 * Tests for DiDPO sub-diff anchors and A2Agent discovery/commitment credit (#48).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	A2_COMMITMENT_WEIGHT,
	A2_DISCOVERY_WEIGHT,
	DiDPOTrainer,
	a2Advantage,
	a2Credit,
	applyDiffInDiff,
	buildAnchors,
	splitSubDiffs,
	type TrajectorySample,
} from "./didpo_trainer";

const DIFF = [
	"diff --git a/src/a.ts b/src/a.ts",
	"@@ -1 +1 @@",
	"-old",
	"+new",
	"diff --git a/src/b.ts b/src/b.ts",
	"@@ -1 +1 @@",
	"-left",
	"+right",
].join("\n");

function sample(id: string, advantage: number): TrajectorySample {
	return {
		id,
		repo: "repo",
		subDiffs: splitSubDiffs(DIFF),
		advantage,
	};
}

describe("sub-diff splitting", () => {
	it("splits a multi-file diff into cohesive sub-diffs", () => {
		const subDiffs = splitSubDiffs(DIFF);
		assert.equal(subDiffs.length, 2);
		assert.deepEqual(subDiffs.map((subDiff) => subDiff.path), ["src/a.ts", "src/b.ts"]);
		assert.equal(subDiffs[0].groupability, 1);
	});

	it("drops hunks below the groupability threshold", () => {
		const noisy = [
			"diff --git a/src/big.ts b/src/big.ts",
			"@@ -1 +1 @@",
			"-a",
			"+b",
			"@@ -10 +10 @@",
			"-c",
			"+d",
			"@@ -20 +20 @@",
			"-e",
			"+f",
		].join("\n");
		assert.deepEqual(splitSubDiffs(noisy), []);
		assert.equal(splitSubDiffs(noisy, 0.3).length, 1);
	});

	it("returns an empty list for an empty diff", () => {
		assert.deepEqual(splitSubDiffs(""), []);
	});
});

describe("cross-trajectory anchors", () => {
	it("groups the same sub-diff path across samples", () => {
		const anchors = buildAnchors([sample("s1", 1), sample("s2", 0)]);
		assert.equal(anchors.length, 2);
		assert.equal(anchors[0].signature, "src/a.ts");
		assert.equal(anchors[0].meanAdvantage, 0.5);
	});

	it("rejects non-finite sample advantages", () => {
		assert.throws(() => buildAnchors([sample("s1", Number.NaN)]), RangeError);
	});

	it("subtracts the anchor mean from each sub-diff", () => {
		const anchors = buildAnchors([sample("s1", 1), sample("s2", 0)]);
		const credit = applyDiffInDiff(sample("s3", 1), anchors);
		assert.equal(credit.perSubDiff.length, 2);
		assert.equal(credit.perSubDiff[0].advantage, 0.5);
		assert.equal(credit.total, 1);
	});

	it("falls back to the sample advantage when no anchor exists", () => {
		const credit = applyDiffInDiff(sample("s1", 1), []);
		assert.equal(credit.perSubDiff[0].advantage, 1);
	});
});

describe("A2Agent localization credit", () => {
	it("separates discovery and commitment", () => {
		assert.equal(A2_DISCOVERY_WEIGHT, 0.4);
		assert.equal(A2_COMMITMENT_WEIGHT, 0.6);
		assert.equal(a2Credit(true, false), 0.4);
		assert.equal(a2Credit(false, true), 0.6);
		assert.equal(a2Credit(true, true), 1);
		assert.equal(a2Credit(false, false), 0);
	});

	it("subtracts the shared exploration baseline", () => {
		assert.equal(a2Advantage(true, true, 0.5), 0.5);
		assert.ok(Math.abs(a2Advantage(true, false, 0.5) + 0.1) < 1e-12);
		assert.throws(() => a2Advantage(true, true, Number.NaN), RangeError);
	});
});

describe("DiDPOTrainer", () => {
	it("fits anchors once and encodes discovery/commitment", () => {
		const trainer = new DiDPOTrainer();
		const anchors = trainer.fit([sample("s1", 1), sample("s2", 0)]);
		assert.equal(anchors.length, 2);
		const credit = trainer.encode(sample("s3", 1), false, true);
		assert.equal(credit.discovery, 0);
		assert.equal(credit.commitment, 0.6);
		assert.equal(credit.total, 0.6);
		assert.equal(credit.perSubDiff[0].advantage, 0.5);
	});

	it("uses the configured A2 baseline", () => {
		const trainer = new DiDPOTrainer();
		trainer.fit([sample("s1", 1), sample("s2", 0)]);
		const credit = trainer.encode(sample("s3", 1), true, true, 0.25);
		assert.equal(credit.total, 0.75);
	});
});
