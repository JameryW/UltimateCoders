/**
 * Tests for SessionFarmer + CropChat closure (#49).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	CropRefiner,
	SessionFarmer,
	balancedAccuracy,
	clusterTurns,
	sealEpisode,
	type RequirementMutant,
	type WorkTurn,
} from "./session_farmer";

function turn(
	id: string,
	sessionId: string,
	file: string,
	commit = "abc123",
	tests?: string[],
): WorkTurn {
	return {
		id,
		sessionId,
		turn: Number(id.replace(/\D/g, "")) || 0,
		request: `request ${id}`,
		files: [file],
		commit,
		tests,
	};
}

describe("session work-episode clustering", () => {
	it("joins turns across sessions when they share a file", () => {
		const episodes = clusterTurns([
			turn("t1", "s1", "src/a.ts"),
			turn("t2", "s2", "src/a.ts"),
			turn("t3", "s3", "src/b.ts"),
		]);
		assert.equal(episodes.length, 2);
		assert.deepEqual(episodes[0].sessions, ["s1", "s2"]);
		assert.deepEqual(episodes[0].files, ["src/a.ts"]);
		assert.deepEqual(episodes[1].sessions, ["s3"]);
	});

	it("keeps unrelated work separate and sorts turns deterministically", () => {
		const episodes = clusterTurns([
			turn("t1", "s1", "src/a.ts"),
			turn("t2", "s2", "src/b.ts"),
			turn("t3", "s1", "src/a.ts"),
		]);
		assert.equal(episodes.length, 2);
		assert.deepEqual(episodes[0].turns.map((item) => item.id), ["t1", "t3"]);
	});

	it("honours a higher shared-file threshold", () => {
		const episodes = clusterTurns(
			[turn("t1", "s1", "src/a.ts"), turn("t2", "s2", "src/b.ts")],
			2,
		);
		assert.equal(episodes.length, 2);
	});

	it("records test coverage and commits in the harvest report", () => {
		const farmer = new SessionFarmer();
		farmer.record(turn("t1", "s1", "src/a.ts", "abc", ["test/a.test.ts"]));
		farmer.record(turn("t2", "s2", "src/a.ts", "def"));
		const report = farmer.harvest();
		assert.equal(report.episodes.length, 1);
		assert.equal(report.testedCommits, 1);
		assert.equal(report.untestedCommits, 1);
		assert.equal(report.sessions, 2);
	});

	it("rejects duplicate turn ids and invalid thresholds", () => {
		const farmer = new SessionFarmer();
		farmer.record(turn("t1", "s1", "src/a.ts"));
		assert.throws(
			() => farmer.record(turn("t1", "s2", "src/b.ts")),
			/duplicate turn id/,
		);
		assert.throws(() => clusterTurns([], 0), /minSharedFiles/);
	});

	it("seals only an episode with a commit", () => {
		const [episode] = clusterTurns([turn("t1", "s1", "src/a.ts", "abc")]);
		assert.equal(sealEpisode(episode, "base").baseCommit, "base");
		assert.throws(
			() =>
				sealEpisode(
					{ ...episode, commits: [] },
					"base",
				),
			/no commit/,
		);
	});
});

describe("CropChat mutual-refinement closure", () => {
	const episode = clusterTurns([turn("t1", "s1", "src/a.ts", "abc")])[0];
	const trueMutant: RequirementMutant = {
		id: "m1",
		description: "reintroduce the cache-key requirement",
		assertion: "cache key includes the namespace",
		expectedResolved: true,
	};
	const falseMutant: RequirementMutant = {
		id: "m2",
		description: "break a different requirement",
		assertion: "lock ordering stays acyclic",
		expectedResolved: false,
	};

	it("reaches perfect balanced accuracy when both gates agree", () => {
		const report = new CropRefiner().refine(
			episode,
			[trueMutant, falseMutant],
			(_, mutant) => ({ testsPassed: mutant.id === "m1" }),
			() => true,
			2,
			3,
		);
		assert.equal(report.trials.length, 12);
		assert.equal(report.balancedAccuracy, 1);
		assert.deepEqual(report.falsePositives, []);
		assert.deepEqual(report.falseNegatives, []);
	});

	it("flags a false positive when a bad patch satisfies both gates", () => {
		const report = new CropRefiner().refine(
			episode,
			[falseMutant],
			() => ({ testsPassed: true }),
			() => true,
			1,
			1,
		);
		assert.equal(report.trials[0].resolved, true);
		assert.equal(report.trials[0].correct, false);
		assert.deepEqual(report.falsePositives, ["m2"]);
		assert.deepEqual(report.falseNegatives, []);
	});

	it("flags a false negative when quality rejects a correct mechanical fix", () => {
		const report = new CropRefiner().refine(
			episode,
			[trueMutant],
			() => ({ testsPassed: true }),
			() => false,
			1,
			1,
		);
		assert.equal(report.trials[0].resolved, false);
		assert.deepEqual(report.falseNegatives, ["m1"]);
		assert.equal(report.balancedAccuracy, 0);
	});

	it("requires both mechanical and quality evidence", () => {
		const report = new CropRefiner().refine(
			episode,
			[trueMutant],
			() => ({ testsPassed: false }),
			() => true,
			1,
			1,
		);
		assert.equal(report.trials[0].mechanicalResolved, false);
		assert.equal(report.trials[0].qualityResolved, true);
		assert.equal(report.trials[0].resolved, false);
	});

	it("computes balanced accuracy with a safe degenerate case", () => {
		const report = new CropRefiner().refine(
			episode,
			[trueMutant],
			() => ({ testsPassed: true }),
			() => true,
			1,
			1,
		);
		assert.equal(balancedAccuracy(report.trials), 1);
		assert.equal(balancedAccuracy([]), 0);
	});

	it("validates rounds, versions, mutants and duplicates", () => {
		const refiner = new CropRefiner();
		assert.throws(() => refiner.refine(episode, [], () => ({ testsPassed: true }), () => true), /mutant/);
		assert.throws(
			() => refiner.refine(episode, [trueMutant], () => ({ testsPassed: true }), () => true, 0, 1),
			/rounds/,
		);
		assert.throws(
			() => refiner.refine(episode, [trueMutant], () => ({ testsPassed: true }), () => true, 1, 0),
			/versions/,
		);
		assert.throws(
			() =>
				refiner.refine(
					episode,
					[trueMutant, { ...trueMutant }],
					() => ({ testsPassed: true }),
					() => true,
					1,
					1,
				),
			/duplicate mutant id/,
		);
	});
});
