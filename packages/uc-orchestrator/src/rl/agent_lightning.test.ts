/**
 * Tests for Agent Lightning v1.0 harness-native RL (#78).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	AGENT_LIGHTNING_SAMPLE_COUNT,
	HarnessLoop,
	QWEN_BASELINE,
	QWEN_WITH_RL,
	SELF_HARNESS_RELATIVE_GAIN,
	SelfHarnessEvolver,
	mineWeaknesses,
	proposeMinimalPatch,
	selectTrajectoryCentroid,
	trainerView,
	verifyRegression,
} from "./agent_lightning";
import type { InteractionSample } from "./agent_lightning";

function sample(id: string, outcome: "pass" | "fail", tags: readonly string[]): InteractionSample {
	return {
		id,
		request: { id, prompt: `prompt ${id}` },
		response: { requestId: id, text: "answer", tokenIds: [1, 2, 3] },
		outcome,
		tags,
		tokenCount: 3,
		turnCount: 1,
		toolCallCount: 0,
	};
}

describe("Agent Lightning constants", () => {
	it("matches the published training metrics", () => {
		assert.equal(AGENT_LIGHTNING_SAMPLE_COUNT, 6000);
		assert.equal(QWEN_BASELINE, 0.418);
		assert.equal(QWEN_WITH_RL, 0.564);
		assert.equal(SELF_HARNESS_RELATIVE_GAIN, 1.32);
	});
});

describe("HarnessLoop", () => {
	it("records raw request/response pairs and exposes a trainer-only view", () => {
		const loop = new HarnessLoop();
		const recorded = loop.record(
			{ id: "r1", prompt: "fix the bug" },
			{ requestId: "r1", text: "done", tokenIds: [1, 2] },
			"fail",
			["missing_test"],
		);
		assert.equal(recorded.outcome, "fail");
		const view = trainerView(recorded);
		assert.deepEqual(view, {
			request: { id: "r1", prompt: "fix the bug" },
			response: { requestId: "r1", text: "done", tokenIds: [1, 2] },
		});
		assert.equal("outcome" in view, false);
		assert.equal("tags" in view, false);
	});

	it("rejects a mismatched response id", () => {
		const loop = new HarnessLoop();
		assert.throws(
			() =>
				loop.record(
					{ id: "r1", prompt: "p" },
					{ requestId: "r2", text: "x", tokenIds: [] },
					"pass",
				),
			/response requestId/,
		);
	});

	it("resets all recorded samples", () => {
		const loop = new HarnessLoop();
		loop.record({ id: "r1", prompt: "p" }, { requestId: "r1", text: "x", tokenIds: [] }, "pass");
		assert.equal(loop.all().length, 1);
		loop.reset();
		assert.equal(loop.all().length, 0);
	});
});

describe("weakness mining and proposals", () => {
	it("ranks failure tags by failure rate", () => {
		const weaknesses = mineWeaknesses([
			sample("a", "fail", ["missing_test"]),
			sample("b", "fail", ["missing_test"]),
			sample("c", "pass", ["missing_test", "slow"]),
			sample("d", "fail", ["slow"]),
		]);
		assert.deepEqual(weaknesses.map((item) => item.tag), ["missing_test", "slow"]);
		assert.equal(weaknesses[0].failures, 2);
		assert.equal(weaknesses[0].total, 3);
	});

	it("proposes a minimal explicit verification step", () => {
		const proposal = proposeMinimalPatch({
			tag: "missing_test",
			failures: 2,
			total: 3,
			failureRate: 2 / 3,
		});
		assert.equal(proposal.id, "patch:missing_test");
		assert.deepEqual(proposal.targetTags, ["missing_test"]);
		assert.match(proposal.change, /verification step/);
	});
});

describe("regression and centroid gates", () => {
	it("passes a candidate inside tolerance and fails a larger regression", () => {
		const proposal = proposeMinimalPatch({ tag: "x", failures: 1, total: 1, failureRate: 1 });
		assert.equal(verifyRegression(proposal, 0.6, 0.59, 0.02).passed, true);
		assert.equal(verifyRegression(proposal, 0.6, 0.5, 0.02).passed, false);
	});

	it("selects 10% of trajectories nearest the centroid", () => {
		const samples = Array.from({ length: 10 }, (_, index) => ({
			id: `s${index}`,
			tokenCount: index + 1,
			turnCount: 1,
			toolCallCount: 0,
		}));
		const subset = selectTrajectoryCentroid(samples, 0.1);
		assert.equal(subset.selected.length, 1);
		assert.ok(["s4", "s5"].includes(subset.selected[0].id));
		assert.ok(subset.medianError >= 0);
	});

	it("returns an empty subset for empty input", () => {
		assert.deepEqual(selectTrajectoryCentroid([]), { selected: [], fraction: 0.1, medianError: 0 });
	});
});

describe("SelfHarnessEvolver", () => {
	it("accepts only proposals with a passing regression score", () => {
		const report = new SelfHarnessEvolver().evolve(
			[sample("a", "fail", ["missing_test"]), sample("b", "pass", ["missing_test"])],
			new Map([["patch:missing_test", { baseline: 0.5, candidate: 0.52 }]]),
		);
		assert.equal(report.proposals.length, 1);
		assert.deepEqual(report.accepted, ["patch:missing_test"]);
		assert.equal(report.subset.selected.length, 1);
	});
});
