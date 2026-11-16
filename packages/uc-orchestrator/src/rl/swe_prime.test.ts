/**
 * Tests for SWE-Prime two-stage semantic segment distillation (#11).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	distillSegments,
	SEGMENT_KINDS,
	selectHighValueTrajectories,
	type TrajectorySegment,
} from "./swe_prime";

function makeSegment(id: string, overrides: Partial<TrajectorySegment> = {}): TrajectorySegment {
	return {
		id,
		kind: "exploration",
		startTurn: 0,
		endTurn: 0,
		reverted: false,
		repeatedIneffective: false,
		positiveCore: true,
		...overrides,
	};
}

describe("distillSegments", () => {
	it("masks reverted and repeated-ineffective segments while keeping positive cores", () => {
		const segments = [
			makeSegment("a", { kind: "exploration", startTurn: 0, endTurn: 1 }),
			makeSegment("b", { kind: "editing", startTurn: 2, endTurn: 3, reverted: true, positiveCore: false }),
			makeSegment("c", { kind: "verification", startTurn: 4, endTurn: 4, repeatedIneffective: true, positiveCore: false }),
			makeSegment("d", { kind: "repair", startTurn: 5, endTurn: 6 }),
		];

		const result = distillSegments(segments);

		assert.deepEqual(result.lossMask, [1, 1, 0, 0, 0, 1, 1]);
		assert.deepEqual(
			result.segments.map((segment) => segment.id),
			["a", "d"],
		);
		assert.equal(result.totalTurns, 7);
		assert.equal(result.keptTurns, 4);
		assert.equal(result.keptRatio, 4 / 7);
	});

	it("collapses adjacent, turn-contiguous segments of the same kind", () => {
		const segments = [
			makeSegment("e1", { kind: "exploration", startTurn: 0, endTurn: 1, positiveCore: false }),
			makeSegment("e2", { kind: "exploration", startTurn: 2, endTurn: 3, positiveCore: true }),
			makeSegment("v1", { kind: "verification", startTurn: 4, endTurn: 4 }),
		];

		const result = distillSegments(segments);

		assert.equal(result.segments.length, 2);
		assert.deepEqual(result.segments[0], {
			id: "e1",
			kind: "exploration",
			startTurn: 0,
			endTurn: 3,
			reverted: false,
			repeatedIneffective: false,
			positiveCore: true,
			metadata: undefined,
		});
		assert.equal(result.segments[1].id, "v1");
	});

	it("does not collapse same-kind segments when another kind separates them or turns are not contiguous", () => {
		const separated = [
			makeSegment("a", { kind: "exploration", startTurn: 0, endTurn: 1 }),
			makeSegment("b", { kind: "editing", startTurn: 2, endTurn: 2 }),
			makeSegment("c", { kind: "exploration", startTurn: 3, endTurn: 4 }),
		];
		const gapped = [
			makeSegment("d", { kind: "editing", startTurn: 0, endTurn: 1 }),
			makeSegment("e", { kind: "editing", startTurn: 3, endTurn: 4 }),
		];

		assert.equal(distillSegments(separated).segments.length, 3);
		assert.equal(distillSegments(gapped).segments.length, 2);
	});

	it("keepPositiveOnly additionally masks non-positive-core segments", () => {
		const segments = [
			makeSegment("a", { startTurn: 0, endTurn: 0, positiveCore: true }),
			makeSegment("b", { startTurn: 1, endTurn: 2, positiveCore: false }),
			makeSegment("c", { startTurn: 3, endTurn: 4, positiveCore: false, reverted: true }),
		];

		const result = distillSegments(segments, { keepPositiveOnly: true });

		assert.deepEqual(result.lossMask, [1, 0, 0, 0, 0]);
		assert.deepEqual(
			result.segments.map((segment) => segment.id),
			["a"],
		);
		assert.equal(result.keptTurns, 1);
		assert.equal(result.keptRatio, 1 / 5);
	});

	it("returns an empty mask and zero ratios for an empty trajectory", () => {
		assert.deepEqual(distillSegments([]), {
			segments: [],
			keptRatio: 0,
			totalTurns: 0,
			keptTurns: 0,
			lossMask: [],
		});
	});

	it("does not mutate the input segments", () => {
		const segment = makeSegment("a", { startTurn: 0, endTurn: 1, metadata: { note: "keep" } });
		const before = structuredClone(segment);

		const result = distillSegments([segment]);

		assert.deepEqual(segment, before);
		assert.notEqual(result.segments[0], segment);
		assert.notEqual(result.segments[0].metadata, segment.metadata);
	});

	it("rejects invalid turn ranges and kinds", () => {
		assert.throws(() => distillSegments([makeSegment("bad", { startTurn: 2, endTurn: 1 })]), RangeError);
		assert.throws(
			() => distillSegments([makeSegment("bad", { kind: "other" as TrajectorySegment["kind"] })]),
			RangeError,
		);
	});
});

describe("selectHighValueTrajectories", () => {
	it("selects the top ratio by score and preserves deterministic ranking", () => {
		const items = [
			{ id: "a", score: 1 },
			{ id: "b", score: 5 },
			{ id: "c", score: 3 },
			{ id: "d", score: 5 },
			{ id: "e", score: 2 },
			{ id: "f", score: 4 },
			{ id: "g", score: 0 },
			{ id: "h", score: 6 },
			{ id: "i", score: 2.5 },
			{ id: "j", score: 3.5 },
		];

		const selected = selectHighValueTrajectories(items, 0.2);

		assert.deepEqual(
			selected.map((item) => item.id),
			["h", "b"],
		);
	});

	it("breaks exact score ties by ascending id", () => {
		const items = [
			{ id: "c", score: 7 },
			{ id: "a", score: 7 },
			{ id: "b", score: 7 },
		];

		const selected = selectHighValueTrajectories(items, 2 / 3);

		assert.deepEqual(
			selected.map((item) => item.id),
			["a", "b"],
		);
	});

	it("supports scored items without ids", () => {
		const items = [{ score: 1 }, { score: 3 }, { score: 2 }];

		assert.deepEqual(selectHighValueTrajectories(items, 1), [{ score: 3 }, { score: 2 }, { score: 1 }]);
	});

	it("handles ratio boundaries and empty input", () => {
		const items = [{ id: "a", score: 1 }, { id: "b", score: 2 }];

		assert.deepEqual(selectHighValueTrajectories(items, 0), []);
		assert.deepEqual(
			selectHighValueTrajectories(items, 1).map((item) => item.id),
			["b", "a"],
		);
		assert.deepEqual(
			selectHighValueTrajectories(items, 99).map((item) => item.id),
			["b", "a"],
		);
		assert.deepEqual(selectHighValueTrajectories(items, -1), []);
		assert.deepEqual(selectHighValueTrajectories([], 0.5), []);
	});

	it("selects at least one item for a positive ratio", () => {
		const items = [{ id: "a", score: 1 }, { id: "b", score: 2 }];

		assert.deepEqual(
			selectHighValueTrajectories(items, 0.01).map((item) => item.id),
			["b"],
		);
	});

	it("rejects non-finite scores and ratios", () => {
		assert.throws(() => selectHighValueTrajectories([{ id: "a", score: Number.NaN }]), RangeError);
		assert.throws(() => selectHighValueTrajectories([{ id: "a", score: Number.POSITIVE_INFINITY }]), RangeError);
		assert.throws(() => selectHighValueTrajectories([{ id: "a", score: 1 }], Number.NaN), RangeError);
	});
});

describe("SEGMENT_KINDS", () => {
	it("lists the five SWE-Prime segment kinds", () => {
		assert.deepEqual(SEGMENT_KINDS, ["exploration", "localization", "editing", "verification", "repair"]);
	});
});
