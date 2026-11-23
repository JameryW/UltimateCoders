/**
 * Tests for HarnessFlow MCTS (#115).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { HarnessFlowError, HarnessFlowMcts } from "./harness_flow_mcts";

const SCORES: Record<string, number> = {
	root: 0.1,
	a: 0.8,
	a2: 0.9,
	b: 0.5,
	c: 0.6,
};

function branch(harness: string): readonly string[] {
	if (harness === "root") return ["a", "b", "c"];
	if (harness === "a") return ["a2"];
	return [];
}

function evaluate(harness: string): number {
	return SCORES[harness] ?? 0;
}

describe("HarnessFlow MCTS", () => {
	it("evaluates refinements and returns the best harness", () => {
		const search = new HarnessFlowMcts(branch, evaluate);
		const report = search.search("root", 15);
		assert.equal(report.best, "a2");
		assert.equal(report.bestScore, 0.9);
		assert.equal(report.evaluations, 5);
		assert.equal(report.nodes.length, 5);
	});

	it("breaks score ties by harness name", () => {
		const search = new HarnessFlowMcts(
			(harness) => (harness === "root" ? ["b", "a"] : []),
			() => 0.5,
		);
		assert.equal(search.search("root", 10).best, "a");
	});

	it("honours the iteration budget", () => {
		const search = new HarnessFlowMcts(
			() => ["a", "b", "c", "d", "e", "f"],
			() => 0.5,
		);
		const report = search.search("root", 3);
		assert.equal(report.evaluations, 3);
		assert.equal(report.nodes.length, 3);
	});

	it("honours the node cap", () => {
		const search = new HarnessFlowMcts(() => ["a", "b", "c"], () => 0.5, 1.414, 2);
		const report = search.search("root", 15);
		assert.equal(report.nodes.length, 2);
	});

	it("rejects an invalid evaluator score", () => {
		const search = new HarnessFlowMcts(() => [], () => 1.5);
		assert.throws(() => search.search("root", 2), HarnessFlowError);
	});

	it("rejects an empty root harness", () => {
		const search = new HarnessFlowMcts(() => [], () => 0.5);
		assert.throws(() => search.search("  ", 2), HarnessFlowError);
	});

	it("rejects invalid iterations", () => {
		const search = new HarnessFlowMcts(() => [], () => 0.5);
		assert.throws(() => search.search("root", 0), HarnessFlowError);
		assert.throws(() => search.search("root", 1.5), HarnessFlowError);
	});

	it("rejects invalid search configuration", () => {
		assert.throws(() => new HarnessFlowMcts(() => [], () => 0.5, -1), HarnessFlowError);
		assert.throws(() => new HarnessFlowMcts(() => [], () => 0.5, 1.414, 1), HarnessFlowError);
	});

	it("returns the root when no refinement exists", () => {
		const search = new HarnessFlowMcts(() => [], () => 0.42);
		const report = search.search("root", 5);
		assert.equal(report.best, "root");
		assert.equal(report.bestScore, 0.42);
		assert.equal(report.evaluations, 1);
	});

	it("ignores duplicate and empty branch candidates", () => {
		const search = new HarnessFlowMcts(
			(harness) => (harness === "root" ? ["a", "a", "  "] : []),
			(harness) => (harness === "a" ? 0.7 : 0.1),
		);
		const report = search.search("root", 5);
		assert.equal(report.best, "a");
		assert.equal(report.nodes.length, 2);
	});
});
