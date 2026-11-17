/**
 * Tests for the TRACE runtime collector (#20).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	normaliseTurnLikelihoods,
	TRACE_LOGPROBS_ENV,
	TraceRuntimeCollector,
	traceLogprobsEnabled,
} from "./trace_runtime";

describe("normaliseTurnLikelihoods", () => {
	it("accepts arrays and JSON-encoded arrays", () => {
		assert.deepEqual(normaliseTurnLikelihoods([0.5, 0.25]), [0.5, 0.25]);
		assert.deepEqual(normaliseTurnLikelihoods("[0.1,0.9]"), [0.1, 0.9]);
	});

	it("drops unusable values instead of substituting defaults", () => {
		assert.deepEqual(normaliseTurnLikelihoods([0.5, 0, -1, 1.2, Number.NaN, "x"]), [0.5]);
		assert.equal(normaliseTurnLikelihoods("not-json"), undefined);
		assert.equal(normaliseTurnLikelihoods([]), undefined);
		assert.equal(normaliseTurnLikelihoods(null), undefined);
	});
});

describe("TraceRuntimeCollector", () => {
	it("buffers observations per task and computes turn credits", () => {
		const collector = new TraceRuntimeCollector();
		collector.record("task-1", [0.8, 0.4]);
		collector.record("task-1", [0.2]);
		collector.record("task-2", [0.5]);
		collector.record("  ", [0.5]);
		collector.record("task-1", []);

		assert.equal(collector.size, 2);
		assert.deepEqual(collector.likelihoods("task-1"), [0.8, 0.4, 0.2]);
		const credits = collector.credits("task-1");
		assert.equal(credits.length, 2);
		assert.equal(credits[0]?.turn, 0);
		assert.equal(collector.credits("task-2").length, 0);
	});

	it("keeps subtask trajectories separate", () => {
		const collector = new TraceRuntimeCollector();
		collector.record("task-1", [0.8, 0.4], "sub-a");
		collector.record("task-1", [0.2, 0.9], "sub-b");

		assert.deepEqual(collector.likelihoods("task-1", "sub-a"), [0.8, 0.4]);
		assert.deepEqual(collector.likelihoods("task-1"), [0.8, 0.4, 0.2, 0.9]);
		const rows = collector.creditsBySubtask("task-1");
		assert.deepEqual(rows.map((row) => row.subtaskId), ["sub-a", "sub-b"]);
		assert.deepEqual(rows.map((row) => row.credits.length), [1, 1]);
		assert.deepEqual(collector.credits("task-1").map((credit) => credit.turn), [0, 0]);
	});

	it("caps per-subtask observations and clears completed tasks", () => {
		const collector = new TraceRuntimeCollector();
		const values = Array.from({ length: 70 }, (_, index) => 0.5 + index / 1000);
		collector.record("task-1", values);
		assert.equal(collector.likelihoods("task-1").length, 64);

		collector.clear("task-1");
		assert.equal(collector.size, 0);
		assert.equal(collector.credits("task-1").length, 0);
	});
});

describe("traceLogprobsEnabled", () => {
	it("accepts common truthy values and rejects blanks", () => {
		const previous = process.env[TRACE_LOGPROBS_ENV];
		try {
			for (const value of ["1", "true", "YES", "on"]) {
				process.env[TRACE_LOGPROBS_ENV] = value;
				assert.equal(traceLogprobsEnabled(), true);
			}
			for (const value of ["", "0", "false", "off"]) {
				process.env[TRACE_LOGPROBS_ENV] = value;
				assert.equal(traceLogprobsEnabled(), false);
			}
			delete process.env[TRACE_LOGPROBS_ENV];
			assert.equal(traceLogprobsEnabled(), false);
		} finally {
			if (previous === undefined) delete process.env[TRACE_LOGPROBS_ENV];
			else process.env[TRACE_LOGPROBS_ENV] = previous;
		}
	});
});
