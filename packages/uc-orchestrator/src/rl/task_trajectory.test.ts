/**
 * Tests for the SWE-Prime orchestrator adapter (#11).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	distillTaskTrajectory,
	phaseKind,
	segmentsFromProgress,
	SWE_PRIME_ENV,
	swePrimeEnabled,
	TaskTrajectoryCollector,
	type TrajectoryProgressEvent,
} from "./task_trajectory";

function progress(
	phase: string,
	overrides: Partial<TrajectoryProgressEvent> = {},
): TrajectoryProgressEvent {
	return {
		taskId: "task-1",
		subtaskId: "task-1-s0",
		phase,
		...overrides,
	};
}

describe("phaseKind", () => {
	it("maps known progress phases to semantic kinds", () => {
		assert.equal(phaseKind("exploration pass"), "exploration");
		assert.equal(phaseKind("localize the failing test"), "localization");
		assert.equal(phaseKind("edit src/app.ts"), "editing");
		assert.equal(phaseKind("run verification"), "verification");
		assert.equal(phaseKind("repair the regression"), "repair");
		assert.equal(phaseKind("step 1/2: local-harness"), "exploration");
	});
});

describe("segmentsFromProgress", () => {
	it("marks earlier duplicate summaries and rollback phases as ineffective", () => {
		const events = [
			progress("exploration pass", { stepStatus: "completed" }),
			progress("edit src/app.ts", { stepStatus: "completed", stepSummary: "changed app" }),
			progress("edit src/app.ts", { stepStatus: "completed", stepSummary: "changed app" }),
			progress("rollback patch", { stepStatus: "completed" }),
			progress("run tests", { stepStatus: "completed" }),
		];

		const segments = segmentsFromProgress(events);

		assert.equal(segments.length, 5);
		assert.equal(segments[1]?.repeatedIneffective, true);
		assert.equal(segments[2]?.repeatedIneffective, false);
		assert.equal(segments[2]?.positiveCore, true);
		assert.equal(segments[3]?.reverted, true);
		assert.equal(segments[4]?.positiveCore, true);

		const distilled = distillTaskTrajectory("task-1", events);
		assert.ok(distilled !== null);
		assert.equal(distilled.segments, 5);
		assert.equal(distilled.keptSegments, 3);
		assert.equal(distilled.lossMask.length, 5);
		assert.equal(distilled.lossMask[1], 0);
		assert.equal(distilled.lossMask[3], 0);
	});
});

describe("distillTaskTrajectory", () => {
	it("does not treat summary-less progress events as duplicates", () => {
		const segments = segmentsFromProgress([
			progress("step 1/2: local-harness"),
			progress("step 2/2: local-harness"),
		]);
		assert.equal(segments.every((segment) => !segment.repeatedIneffective), true);
	});

	it("honours keepPositiveOnly and returns a compact summary", () => {
		const events = [
			progress("exploration pass"),
			progress("edit src/app.ts", { stepStatus: "completed" }),
			progress("run tests", { stepStatus: "completed" }),
		];

		const all = distillTaskTrajectory("task-1", events);
		const positive = distillTaskTrajectory("task-1", events, { keepPositiveOnly: true });

		assert.ok(all !== null);
		assert.ok(positive !== null);
		assert.equal(all.keptSegments, 3);
		assert.equal(positive.keptSegments, 2);
		assert.deepEqual(positive.kinds, ["editing", "verification"]);
	});

	it("rejects empty task ids and returns null for no events", () => {
		assert.throws(() => distillTaskTrajectory("   ", []), /taskId/);
		assert.equal(distillTaskTrajectory("task-1", []), null);
	});
});

describe("TaskTrajectoryCollector", () => {
	it("buffers by task, distils, and clears", () => {
		const collector = new TaskTrajectoryCollector();
		collector.record(progress("exploration pass"));
		collector.record(progress("edit src/app.ts", { stepStatus: "completed" }));
		collector.record(progress("ignored", { taskId: "  " }));

		assert.equal(collector.size, 1);
		const summary = collector.distill("task-1");
		assert.ok(summary !== null);
		assert.equal(summary.taskId, "task-1");
		assert.equal(summary.segments, 2);

		collector.clear("task-1");
		assert.equal(collector.size, 0);
		assert.equal(collector.distill("task-1"), null);
	});

	it("caps the per-task buffer without throwing", () => {
		const collector = new TaskTrajectoryCollector();
		for (let index = 0; index < 900; index++) {
			collector.record(progress(`edit step ${index}`, { stepStatus: "completed" }));
		}
		const summary = collector.distill("task-1");
		assert.ok(summary !== null);
		assert.equal(summary.segments, 800);
	});
});

describe("swePrimeEnabled", () => {
	it("accepts common truthy values and rejects blanks", () => {
		const previous = process.env[SWE_PRIME_ENV];
		try {
			for (const value of ["1", "true", "YES", "on"]) {
				process.env[SWE_PRIME_ENV] = value;
				assert.equal(swePrimeEnabled(), true);
			}
			for (const value of ["", "0", "false", "off"]) {
				process.env[SWE_PRIME_ENV] = value;
				assert.equal(swePrimeEnabled(), false);
			}
			delete process.env[SWE_PRIME_ENV];
			assert.equal(swePrimeEnabled(), false);
		} finally {
			if (previous === undefined) delete process.env[SWE_PRIME_ENV];
			else process.env[SWE_PRIME_ENV] = previous;
		}
	});
});
