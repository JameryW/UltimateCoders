import { describe, expect, test } from "bun:test";

import {
	CodeNibContext,
	SkillBuffBench,
	behaviorProfile,
	compareProfiles,
	fnv1a,
} from "./behavior_fingerprint";

describe("behavioral trajectory fingerprints", () => {
	test("counts phases and ignores tool names and outcomes", () => {
		const profile = behaviorProfile([
			{ phase: "search" },
			{ phase: "search" },
			{ phase: "edit" },
			{ phase: "test" },
		]);
		expect(profile.searches).toBe(2);
		expect(profile.edits).toBe(1);
		expect(profile.tests).toBe(1);
		expect(profile.transitions).toBe("SSET");
	});

	test("identical profiles have identical fingerprints", () => {
		const left = behaviorProfile([{ phase: "search" }, { phase: "edit" }]);
		const right = behaviorProfile([{ phase: "search" }, { phase: "edit" }]);
		expect(left.fingerprint).toBe(right.fingerprint);
		expect(compareProfiles(left, right)).toBe(1);
	});

	test("different profiles compare below one", () => {
		const left = behaviorProfile([{ phase: "search" }]);
		const right = behaviorProfile([{ phase: "test" }]);
		expect(compareProfiles(left, right)).toBeLessThan(1);
	});

	test("fnv1a is deterministic and eight hex digits", () => {
		expect(fnv1a("hello")).toBe(fnv1a("hello"));
		expect(fnv1a("hello")).toMatch(/^[0-9a-f]{8}$/);
	});
});

describe("CodeNib multi-view context", () => {
	test("stores and renders repository views", () => {
		const context = new CodeNibContext();
		context.addView({ path: "src/a.ts", symbols: ["A"], dependencies: ["b"] });
		expect(context.size).toBe(1);
		expect(context.render(["src/a.ts", "missing"])).toContain("src/a.ts");
	});
});

describe("SkillBuffBench paired bootstrap", () => {
	const samples = [
		{ taskId: "t1", skill: "auto" as const, memory: "on" as const, score: 0.8 },
		{ taskId: "t1", skill: "curated" as const, memory: "on" as const, score: 0.6 },
		{ taskId: "t2", skill: "auto" as const, memory: "on" as const, score: 0.9 },
		{ taskId: "t2", skill: "curated" as const, memory: "on" as const, score: 0.7 },
	];

	test("computes paired delta and confidence interval", () => {
		const comparison = new SkillBuffBench(samples).compare("auto", "curated", "on");
		expect(comparison.pairs).toBe(2);
		expect(comparison.delta).toBeCloseTo(0.2);
		expect(comparison.ciLow).toBeLessThanOrEqual(comparison.delta);
		expect(comparison.ciHigh).toBeGreaterThanOrEqual(comparison.delta);
	});

	test("returns zero for no pairs", () => {
		const comparison = new SkillBuffBench(samples).compare("none", "curated", "on");
		expect(comparison.pairs).toBe(0);
		expect(comparison.delta).toBe(0);
	});
});
