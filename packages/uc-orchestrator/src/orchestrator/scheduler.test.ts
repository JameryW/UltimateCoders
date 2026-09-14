/**
 * Scheduler + TaskStore self-check — validates DAG construction and persistence logic.
 *
 * T6 #642 C4: the wave-splitter / FileIntentTracker / checkpoint / recoverable
 * suites were removed with the wave machine they tested (PRD-mandated deletes;
 * recorded in implement.jsonl). buildDAG survives as decomposer-output
 * validation; normalizeFileIntent now feeds C5's conflict_risk grading.
 *
 * Run: bun test src/orchestrator/scheduler.test.ts
 */

import { beforeEach, describe, expect, it } from "bun:test";
import { buildDAG, classifyConflicts, detectCycles, CircuitBreaker, fileOverlapRatio, normalizeFileIntent, recursiveDecompose, type SubtaskDef } from "./scheduler";
import { TaskStore, type PersistedTask } from "./task-store";

function st(id: string, description: string, dependsOn: string[] = [], files: string[] = []): SubtaskDef {
	return { id, description, dependsOn, files };
}

describe("detectCycles", () => {
	it("returns null for acyclic graph", () => {
		const subtasks = [
			st("a", "task a"),
			st("b", "task b", ["a"]),
			st("c", "task c", ["b"]),
		];
		expect(detectCycles(subtasks)).toBeNull();
	});

	it("detects direct cycle", () => {
		const subtasks = [
			st("a", "task a", ["b"]),
			st("b", "task b", ["a"]),
		];
		const cycles = detectCycles(subtasks);
		expect(cycles).not.toBeNull();
		expect(cycles!.length).toBe(2);
	});

	it("returns null for independent subtasks", () => {
		const subtasks = [
			st("a", "task a"),
			st("b", "task b"),
			st("c", "task c"),
		];
		expect(detectCycles(subtasks)).toBeNull();
	});
});

describe("buildDAG", () => {
	it("creates single wave for independent subtasks", () => {
		const subtasks = [st("a", "a"), st("b", "b"), st("c", "c")];
		const waves = buildDAG(subtasks);
		expect(waves.length).toBe(1);
		expect(waves[0].length).toBe(3);
	});

	it("creates sequential waves for chain", () => {
		const subtasks = [
			st("a", "a"),
			st("b", "b", ["a"]),
			st("c", "c", ["b"]),
		];
		const waves = buildDAG(subtasks);
		expect(waves.length).toBe(3);
		expect(waves[0].map((s) => s.id)).toEqual(["a"]);
		expect(waves[1].map((s) => s.id)).toEqual(["b"]);
		expect(waves[2].map((s) => s.id)).toEqual(["c"]);
	});

	it("creates diamond pattern (fan-out then fan-in)", () => {
		const subtasks = [
			st("a", "a"),
			st("b", "b", ["a"]),
			st("c", "c", ["a"]),
			st("d", "d", ["b", "c"]),
		];
		const waves = buildDAG(subtasks);
		expect(waves.length).toBe(3);
		expect(waves[0].map((s) => s.id)).toEqual(["a"]);
		expect(new Set(waves[1].map((s) => s.id))).toEqual(new Set(["b", "c"]));
		expect(waves[2].map((s) => s.id)).toEqual(["d"]);
	});

	it("throws on cycle", () => {
		const subtasks = [
			st("a", "a", ["b"]),
			st("b", "b", ["a"]),
		];
		expect(() => buildDAG(subtasks)).toThrow("Circular dependencies");
	});

	it("throws on missing dependency", () => {
		const subtasks = [st("a", "a", ["nonexistent"])];
		expect(() => buildDAG(subtasks)).toThrow("does not exist");
	});

	it("handles single subtask", () => {
		const waves = buildDAG([st("a", "a")]);
		expect(waves.length).toBe(1);
		expect(waves[0].length).toBe(1);
	});
});

// ── TaskStore tests ────────────────────────────────────────────────

describe("TaskStore", () => {
	// ponytail: unique dir per test to avoid cross-test leakage
	let testDir: string;

	function makeTask(overrides?: Partial<PersistedTask>): PersistedTask {
		return {
			id: "uc-1-test",
			description: "test task",
			status: "in_progress",
			controlState: "running",
			subtasks: [
				{ id: "st-1", description: "subtask 1", status: "completed", dependsOn: [], files: [] },
				{ id: "st-2", description: "subtask 2", status: "pending", dependsOn: ["st-1"], files: [] },
			],
			createdAt: Date.now(),
			...overrides,
		};
	}

	// Fresh directory per test
	beforeEach(() => {
		testDir = `/tmp/uc-test-tasks-${Date.now()}-${Math.random().toString(36).slice(2)}`;
	});

	it("saves and loads a task", async () => {
		const store = new TaskStore(testDir);
		await store.init();

		const task = makeTask();
		await store.save(task);

		const loaded = await store.load(task.id);
		expect(loaded).not.toBeNull();
		expect(loaded!.id).toBe(task.id);
		expect(loaded!.status).toBe("in_progress");
		expect(loaded!.subtasks.length).toBe(2);
	});

	it("returns null for nonexistent task", async () => {
		const store = new TaskStore(testDir);
		await store.init();

		const loaded = await store.load("nonexistent");
		expect(loaded).toBeNull();
	});

	it("loads all tasks", async () => {
		const store = new TaskStore(testDir);
		await store.init();

		await store.save(makeTask({ id: "uc-1-a" }));
		await store.save(makeTask({ id: "uc-1-b", status: "completed" }));

		const all = await store.loadAll();
		expect(all.length).toBe(2);
	});

	it("removes a task", async () => {
		const store = new TaskStore(testDir);
		await store.init();

		const task = makeTask({ id: "uc-remove-me" });
		await store.save(task);
		expect(await store.load(task.id)).not.toBeNull();

		await store.remove(task.id);
		expect(await store.load(task.id)).toBeNull();
	});

		it("handles empty directory gracefully", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			expect(await store.loadAll()).toEqual([]);
		});

		it("overwrites existing task on save", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			await store.save(makeTask({ id: "uc-1", status: "in_progress" }));
			await store.save(makeTask({ id: "uc-1", status: "completed" }));

			const loaded = await store.load("uc-1");
			expect(loaded!.status).toBe("completed");
		});

		it("persists subtask results and reviews", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			const task = makeTask({
				id: "uc-with-results",
				subtasks: [
				{
					id: "st-1",
					description: "subtask 1",
					status: "completed",
					dependsOn: [],
					files: ["src/main.ts"],
					result: "All tests pass",
					review: { approved: true, issues: [], suggestions: ["Add more tests"] },
					startedAt: 1000,
					completedAt: 2000,
				},
				],
			});
			await store.save(task);

			const loaded = await store.load("uc-with-results");
			expect(loaded!.subtasks[0].result).toBe("All tests pass");
			expect(loaded!.subtasks[0].review?.approved).toBe(true);
			expect(loaded!.subtasks[0].review?.suggestions).toEqual(["Add more tests"]);
			expect(loaded!.subtasks[0].startedAt).toBe(1000);
			expect(loaded!.subtasks[0].completedAt).toBe(2000);
		});

		// ── wave-checkpoint machinery (resumeFromWave) removed with the wave
		// machine — T6 #642 C4. The persist/load contract it covered is still
		// locked by the files/steps round-trip tests below.

		it("persists and restores subtask files", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			const task = makeTask({
				id: "uc-files-persist",
				subtasks: [
					{ id: "st-1", description: "subtask 1", status: "pending", dependsOn: [], files: ["a.ts", "b.ts"] },
					{ id: "st-2", description: "subtask 2", status: "pending", dependsOn: ["st-1"], files: ["c.ts"] },
				],
			});
			await store.save(task);

			const loaded = await store.load("uc-files-persist");
			expect(loaded!.subtasks[0].files).toEqual(["a.ts", "b.ts"]);
			expect(loaded!.subtasks[1].files).toEqual(["c.ts"]);
		});

		// ponytail: steps round-trip — locks the SubtaskDef.steps → persist → load
		// contract. If steps don't survive persistence, resumed tasks run empty
		// step chains (single-agent fallback) silently. This is the one check that
		// fails if the new steps field is dropped from serialization anywhere.
		it("persists and restores subtask workflow steps", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			const task = makeTask({
				id: "uc-steps-persist",
				subtasks: [
					{
						id: "st-1",
						description: "implement auth middleware",
						status: "pending",
						dependsOn: [],
						files: ["src/auth/middleware.ts"],
						steps: [
							{ agent: "claude-code", prompt: "Implement JWT auth middleware.", abort_on_failure: true },
							{ agent: "codex", prompt: "Review changes. Prev: {{prev_summary}} files: {{prev_files}}" },
							{ agent: "claude-code", prompt: "Revise per CR. Prev: {{prev_summary}}", abort_on_failure: false },
						],
					},
				],
			});
			await store.save(task);

			const loaded = await store.load("uc-steps-persist");
			const st = loaded!.subtasks[0];
			expect(st.steps).toBeDefined();
			expect(st.steps!.length).toBe(3);
			expect(st.steps![0].agent).toBe("claude-code");
			expect(st.steps![0].prompt).toBe("Implement JWT auth middleware.");
			expect(st.steps![0].abort_on_failure).toBe(true);
			expect(st.steps![1].agent).toBe("codex");
			expect(st.steps![1].prompt).toContain("{{prev_summary}}");
			expect(st.steps![1].abort_on_failure).toBeUndefined();
			expect(st.steps![2].abort_on_failure).toBe(false);
		});

		it("steps undefined when not set (backward compatible)", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			const task = makeTask({ id: "uc-no-steps" });
			await store.save(task);

			const loaded = await store.load("uc-no-steps");
			expect(loaded!.subtasks[0].steps).toBeUndefined();
		});
});

// ── CircuitBreaker tests ────────────────────────────────────────────

describe("CircuitBreaker", () => {
	it("starts in closed state", () => {
		const cb = new CircuitBreaker();
		expect(cb.canExecute()).toBe(true);
		expect(cb.getState()).toBe("closed");
	});

	it("opens after threshold consecutive failures", () => {
		const cb = new CircuitBreaker(3, 1000);
		cb.recordFailure();
		cb.recordFailure();
		expect(cb.canExecute()).toBe(true); // still closed
		cb.recordFailure();
		expect(cb.canExecute()).toBe(false); // now open
		expect(cb.getState()).toBe("open");
	});

	it("resets to closed on success", () => {
		const cb = new CircuitBreaker(2, 1000);
		cb.recordFailure();
		cb.recordFailure();
		expect(cb.getState()).toBe("open");
		cb.recordSuccess();
		expect(cb.getState()).toBe("closed");
		expect(cb.canExecute()).toBe(true);
	});

	it("transitions to half_open after reset timeout", () => {
		const cb = new CircuitBreaker(2, 50); // 50ms reset
		cb.recordFailure();
		cb.recordFailure();
		expect(cb.getState()).toBe("open");
		// Wait for reset timeout
		return new Promise<void>((resolve) => {
			setTimeout(() => {
				expect(cb.canExecute()).toBe(true);
				expect(cb.getState()).toBe("half_open");
				resolve();
			}, 60);
		});
	});

	it("reset() clears all state", () => {
		const cb = new CircuitBreaker(2, 1000);
		cb.recordFailure();
		cb.recordFailure();
		cb.reset();
		expect(cb.getState()).toBe("closed");
		expect(cb.canExecute()).toBe(true);
	});
});

// ponytail: F47 — decomposer files arrive unnormalized; raw string equality
// missed same-file variants. T6 #642 C4: the runtime consumers (wave splitter
// + FileIntentTracker) are gone — the normalizer now feeds C5's conflict_risk
// grading at decomposition time (see classifyConflicts).
describe("file intent normalization (F47)", () => {
	it("normalizes ./ prefixes and redundant separators", () => {
		expect(normalizeFileIntent("./src/a.ts")).toBe(normalizeFileIntent("src/a.ts"));
		expect(normalizeFileIntent("src//a.ts")).toBe(normalizeFileIntent("src/a.ts"));
		expect(normalizeFileIntent("src/b/../a.ts")).toBe(normalizeFileIntent("src/a.ts"));
	});

	it("case-folds on case-insensitive platforms only", () => {
		if (process.platform === "darwin" || process.platform === "win32") {
			expect(normalizeFileIntent("src/A.ts")).toBe(normalizeFileIntent("src/a.ts"));
		} else {
			expect(normalizeFileIntent("src/A.ts")).not.toBe(normalizeFileIntent("src/a.ts"));
		}
	});
});

// T6 #642 C5 — the wave machine's hard file-disjoint waves became a graded
// signal consumed by the claim loop's batch gate. The thresholds and the
// min-set normalization are the contract: 1 shared of 3 grades by the
// smaller set, so a single-file node touching a big node's file is HIGH
// (fully covered → last-write-wins risk), not a rounding footnote.
describe("conflict risk grading (C5)", () => {
	it("fileOverlapRatio: disjoint/empty are 0, min-normalization is symmetric, F47 variants collide", () => {
		expect(fileOverlapRatio(["src/a.ts"], ["src/b.ts"])).toBe(0);
		// Empty file sets are always safe (the old wave splitter's rule).
		expect(fileOverlapRatio([], ["src/a.ts"])).toBe(0);
		expect(fileOverlapRatio(["src/a.ts"], [])).toBe(0);
		// min-set normalization: 1 shared of 3 either direction.
		expect(fileOverlapRatio(["a", "b", "c"], ["a", "x", "y"])).toBeCloseTo(1 / 3);
		expect(fileOverlapRatio(["a", "x", "y"], ["a", "b", "c"])).toBeCloseTo(1 / 3);
		// LLM files arrays dedupe before grading.
		expect(fileOverlapRatio(["a", "a"], ["a"])).toBe(1);
		if (process.platform === "darwin" || process.platform === "win32") {
			// F47: "./src/a.ts" and "src/A.ts" are the same file here.
			expect(fileOverlapRatio(["./src/a.ts"], ["src/A.ts"])).toBe(1);
		}
	});

	it("classifies by worst pairwise overlap: low / medium(0.4) / high(0.8)", () => {
		// All disjoint → unconstrained.
		const free = [{ id: "a", files: ["a.ts"] }, { id: "b", files: ["b.ts"] }];
		expect([...classifyConflicts(free).values()]).toEqual(["low", "low"]);
		// Full overlap → both high.
		const hot = [{ id: "a", files: ["a.ts", "b.ts"] }, { id: "b", files: ["a.ts", "b.ts"] }];
		expect([...classifyConflicts(hot).values()]).toEqual(["high", "high"]);
		// Partial overlap (1/2 = 0.5) → both medium.
		const warm = [{ id: "a", files: ["a.ts", "b.ts"] }, { id: "b", files: ["a.ts", "c.ts"] }];
		expect([...classifyConflicts(warm).values()]).toEqual(["medium", "medium"]);
		// A lone node has no peers → low regardless of its file count.
		expect([...classifyConflicts([{ id: "a", files: ["a.ts", "b.ts"] }]).values()]).toEqual(["low"]);
	});

	it("honors the exact 0.4/0.8 thresholds (>= is inclusive)", () => {
		// 2 shared of 5 = 0.4 exactly → medium.
		const atMedium = [
			{ id: "a", files: ["f1", "f2", "f3", "f4", "f5"] },
			{ id: "b", files: ["f1", "f2", "g1", "g2", "g3"] },
		];
		expect([...classifyConflicts(atMedium).values()]).toEqual(["medium", "medium"]);
		// 4 shared of 5 = 0.8 exactly → high.
		const atHigh = [
			{ id: "a", files: ["f1", "f2", "f3", "f4", "f5"] },
			{ id: "b", files: ["f1", "f2", "f3", "f4", "g1"] },
		];
		expect([...classifyConflicts(atHigh).values()]).toEqual(["high", "high"]);
		// 3 shared of 5 = 0.6 → medium, and the pair stays symmetric.
		const mid = [
			{ id: "a", files: ["f1", "f2", "f3", "f4", "f5"] },
			{ id: "b", files: ["f1", "f2", "f3", "g1", "g2"] },
		];
		expect([...classifyConflicts(mid).values()]).toEqual(["medium", "medium"]);
	});
});

// ponytail: F48 — duplicate ids used to surface as "Circular dependencies
// detected: []" or a deadlock; duplicate dependsOn entries deadlocked the
// wave loop (inDegree over-counted vs the deduped dependents set).
describe("buildDAG input validation (F48)", () => {
	it("duplicate subtask ids throw an explicit error, not a bogus cycle", () => {
		expect(() => buildDAG([st("a", "a"), st("a", "a-dup")])).toThrow(/Duplicate subtask id "a"/);
	});

	it("duplicate dependsOn entries don't deadlock", () => {
		const waves = buildDAG([st("a", "a"), st("b", "b", ["a", "a", "a"])]);
		expect(waves.length).toBe(2);
		expect(waves[1].map((s) => s.id)).toEqual(["b"]);
	});
});

// ponytail: F49 — recursiveDecompose used to emit children[1..n] BEFORE the
// wave containing children[0], so dependent children ran before their
// dependency. File-split children (all depend only on c0) must run parallel.
describe("recursiveDecompose wave ordering (F49)", () => {
	it("file-split: c0 takes parent slot, siblings run parallel after", () => {
		const parent = st("p", "parent", [], ["f1.ts", "f2.ts", "f3.ts"]); // ≥3 files → decomposes
		const out = recursiveDecompose([[parent]]);
		expect(out.map((w) => w.map((s) => s.id))).toEqual([
			["p-f0"],
			["p-f1", "p-f2"], // parallel — both depend only on p-f0
		]);
	});

	it("description-split: chain children stay strictly ordered after c0", () => {
		const longDesc = ["a".repeat(70), "b".repeat(70), "c".repeat(70)].join("\n"); // >200 → decomposes
		const parent = st("p", longDesc);
		const out = recursiveDecompose([[parent]]);
		expect(out.map((w) => w.map((s) => s.id))).toEqual([
			["p-p0"],
			["p-p1"],
			["p-p2"],
		]);
	});
});
