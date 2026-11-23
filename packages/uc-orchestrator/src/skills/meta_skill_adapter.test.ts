import { describe, expect, test } from "bun:test";

import {
	MetaSkillAdapter,
	obstaclesForTier,
	type SkillDocument,
} from "./meta_skill_adapter";

const skill: SkillDocument = {
	name: "cache-repair",
	body: "Find the cache invalidation bug and repair it.",
};

describe("executor-tier obstacle selection", () => {
	test("flagship receives no execution contract", () => {
		expect(obstaclesForTier("flagship")).toEqual([]);
	});

	test("mid receives the three execution-facing sections", () => {
		expect(obstaclesForTier("mid")).toEqual([
			"tool_execution",
			"context_management",
			"instruction_following",
		]);
	});

	test("small receives all four sections", () => {
		expect(obstaclesForTier("small")).toEqual([
			"tool_execution",
			"context_management",
			"instruction_following",
			"verification",
		]);
	});
});

describe("MetaSkillAdapter", () => {
	test("leaves a flagship skill untouched", () => {
		const adapted = new MetaSkillAdapter().rewrite(skill, "flagship");
		expect(adapted.body).toBe(skill.body);
		expect(adapted.obstacles).toEqual([]);
	});

	test("adds the contract marker for a mid-tier executor", () => {
		const adapted = new MetaSkillAdapter().rewrite(skill, "mid");
		expect(adapted.body).toContain("## Executor-specific execution contract");
		expect(adapted.body).toContain("### Tool execution");
		expect(adapted.body).toContain("### Context management");
		expect(adapted.body).toContain("### Instruction following");
		expect(adapted.body).not.toContain("### Verification");
	});

	test("adds verification guidance for a small executor", () => {
		const adapted = new MetaSkillAdapter().rewrite(skill, "small");
		expect(adapted.body).toContain("### Verification");
		expect(adapted.body).toContain("Run the regression suite");
		expect(adapted.body).toContain("unexecuted test");
	});

	test("small-tier guidance is more granular than mid-tier", () => {
		const mid = new MetaSkillAdapter().rewrite(skill, "mid").body;
		const small = new MetaSkillAdapter().rewrite(skill, "small").body;
		expect(small.length).toBeGreaterThan(mid.length);
		expect(small).toContain("Call exactly one tool per step.");
		expect(mid).toContain("Use one tool call per step");
	});

	test("re-adapting replaces rather than duplicates the contract", () => {
		const adapter = new MetaSkillAdapter();
		const once = adapter.rewrite(skill, "mid");
		const twice = adapter.rewrite(once, "small");
		expect(twice.body.match(/## Executor-specific execution contract/g)).toHaveLength(1);
		expect(twice.body).toContain("### Verification");
	});

	test("preserves the original skill body before the contract", () => {
		const adapted = new MetaSkillAdapter().rewrite(skill, "mid");
		expect(adapted.body.startsWith(skill.body)).toBe(true);
	});

	test("adapts a batch and keeps the source order", () => {
		const adapted = new MetaSkillAdapter().adaptMany(
			[skill, { name: "lint", body: "Run the linter." }],
			"small",
		);
		expect(adapted.map((item) => item.name)).toEqual(["cache-repair", "lint"]);
		expect(adapted.every((item) => item.tier === "small")).toBe(true);
	});

	test("rejects empty skill names and bodies", () => {
		expect(() =>
			new MetaSkillAdapter().rewrite({ name: "", body: "x" }, "mid"),
		).toThrow("skill name");
		expect(() =>
			new MetaSkillAdapter().rewrite({ name: "x", body: "" }, "mid"),
		).toThrow("skill body");
	});

	test("reports the obstacles it added", () => {
		const adapted = new MetaSkillAdapter().rewrite(skill, "small");
		expect(adapted.obstacles).toEqual([
			"tool_execution",
			"context_management",
			"instruction_following",
			"verification",
		]);
	});
});
