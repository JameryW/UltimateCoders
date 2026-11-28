/**
 * Tests for XRepoSkill distillation and task-family harness routing (#47).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	DEFAULT_HARNESS_PROFILES,
	TaskFamilyHarnessRouter,
	XRepoSkillDistiller,
	evaluatePredicate,
	firstDivergence,
	type SkillTrace,
} from "./xreposkill_distiller";

function success(taskId = "task-1", repo = "repo-a"): SkillTrace {
	return {
		taskId,
		family: "repair",
		repo,
		steps: ["reproduce", "read src/cache.py", "patch Store.purge", "run tests"],
		outcome: "success",
	};
}

function failure(taskId = "task-1", repo = "repo-a"): SkillTrace {
	return {
		taskId,
		family: "repair",
		repo,
		steps: ["reproduce", "read src/cache.py", "edit src/store.py blindly", "run tests"],
		outcome: "failure",
	};
}

function candidate() {
	const distiller = new XRepoSkillDistiller();
	distiller.record(success());
	distiller.record(failure());
	const [value] = distiller.candidates();
	assert.ok(value);
	return value;
}

describe("task-family harness routing", () => {
	it("keeps four isolated default profiles", () => {
		const router = new TaskFamilyHarnessRouter();
		assert.deepEqual(router.families(), ["feature", "refactor", "repair", "test-gen"]);
		assert.equal(router.resolve("repair").verification, "failing-test-first");
		assert.equal(router.resolve("feature").budget, 40);
		assert.notEqual(router.resolve("repair").verification, router.resolve("refactor").verification);
	});

	it("updates one family without leaking into another", () => {
		const router = new TaskFamilyHarnessRouter();
		const originalRepair = router.resolve("repair");
		router.register({
			family: "feature",
			verification: "dual-spec",
			workflow: ["spec", "implement"],
			budget: 99,
		});
		assert.equal(router.resolve("feature").budget, 99);
		assert.deepEqual(router.resolve("repair"), originalRepair);
		assert.equal(DEFAULT_HARNESS_PROFILES.repair.budget, 30);
	});

	it("rejects invalid profiles", () => {
		const router = new TaskFamilyHarnessRouter();
		assert.throws(
			() =>
				router.register({
					family: "repair",
					verification: "",
					workflow: [],
					budget: 1,
				}),
			RangeError,
		);
	});
});

describe("executable predicates", () => {
	it("evaluates numeric comparisons and missing evidence", () => {
		assert.equal(evaluatePredicate({ field: "errorCount", op: ">=", value: 1 }, { errorCount: 1 }), true);
		assert.equal(evaluatePredicate({ field: "testPassRate", op: "<", value: 0.5 }, { testPassRate: 0.49 }), true);
		assert.equal(evaluatePredicate({ field: "changedFiles", op: "==", value: 2 }, { changedFiles: 3 }), false);
		assert.equal(evaluatePredicate({ field: "tokenCount", op: "!=", value: 0 }, {}), false);
	});
});

describe("trace divergence and candidate extraction", () => {
	it("finds the first differing action", () => {
		const fork = firstDivergence(success(), failure());
		assert.ok(fork);
		assert.equal(fork.step, 2);
		assert.equal(fork.after, "patch Store.purge");
		assert.equal(fork.before, "edit src/store.py blindly");
	});

	it("returns null for identical traces and rejects mixed task ids", () => {
		assert.equal(firstDivergence(success(), { ...failure(), steps: success().steps }), null);
		assert.throws(() => firstDivergence(success("a"), failure("b")), RangeError);
	});

	it("extracts one deduplicated candidate and stable statement", () => {
		const distiller = new XRepoSkillDistiller();
		distiller.record(success());
		distiller.record(failure());
		const candidates = distiller.candidates();
		assert.equal(candidates.length, 1);
		assert.equal(candidates[0].fork.step, 2);
		assert.match(candidates[0].statement, /prefer `patch Store\.purge`/);
		assert.equal(candidates[0].predicate.field, "errorCount");
	});

	it("ignores unpaired traces and invalid records", () => {
		const distiller = new XRepoSkillDistiller();
		distiller.record(success());
		assert.deepEqual(distiller.candidates(), []);
		assert.throws(
			() => distiller.record({ ...success(), steps: [] }),
			RangeError,
		);
	});
});

describe("cross-repository skill validation", () => {
	it("keeps a skill with positive gains across two repositories", () => {
		const value = candidate();
		const evaluations = new XRepoSkillDistiller().evaluate(
			[value],
			[
				{ candidateId: value.id, repo: "repo-a", gain: 1 },
				{ candidateId: value.id, repo: "repo-b", gain: 0.5 },
			],
		);
		assert.equal(evaluations.length, 1);
		assert.equal(evaluations[0].repos.length, 2);
		assert.equal(evaluations[0].meanGain, 0.75);
		assert.equal(evaluations[0].keep, true);
	});

	it("rejects a skill validated in only one repository", () => {
		const value = candidate();
		const [evaluation] = new XRepoSkillDistiller().evaluate(
			[value],
			[{ candidateId: value.id, repo: "repo-a", gain: 1 }],
		);
		assert.equal(evaluation.keep, false);
	});

	it("rejects a skill whose cross-repository gain is non-positive or noisy", () => {
		const value = candidate();
		const [negative] = new XRepoSkillDistiller().evaluate(
			[value],
			[
				{ candidateId: value.id, repo: "repo-a", gain: 1 },
				{ candidateId: value.id, repo: "repo-b", gain: -0.5 },
			],
		);
		assert.equal(negative.keep, false);
		const [noisy] = new XRepoSkillDistiller().evaluate(
			[value],
			[
				{ candidateId: value.id, repo: "repo-a", gain: 1 },
				{ candidateId: value.id, repo: "repo-b", gain: -0.2 },
			],
		);
		assert.equal(noisy.keep, false);
	});
});
