/**
 * Scheduler + TaskStore self-check — validates DAG construction and persistence logic.
 *
 * Run: bun test src/orchestrator/scheduler.test.ts
 */

import { beforeEach, describe, expect, it } from "bun:test";
import { buildDAG, detectCycles, splitWavesByFileOverlap, FileIntentTracker, type SubtaskDef } from "./scheduler";
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

	it("filters recoverable tasks", async () => {
		const store = new TaskStore(testDir);
		await store.init();

		await store.save(makeTask({ id: "uc-recoverable", status: "in_progress", controlState: "running" }));
		await store.save(makeTask({ id: "uc-paused", status: "in_progress", controlState: "paused" }));
		await store.save(makeTask({ id: "uc-done", status: "completed", controlState: "running" }));
		await store.save(makeTask({ id: "uc-failed", status: "failed", controlState: "running" }));

		const recoverable = await store.loadRecoverable();
		expect(recoverable.length).toBe(3);
		expect(recoverable.map((t) => t.id).sort()).toEqual(["uc-failed", "uc-paused", "uc-recoverable"]);
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

		it("recovers planning tasks", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			await store.save(makeTask({ id: "uc-planning", status: "planning", controlState: "running" }));

			const recoverable = await store.loadRecoverable();
			expect(recoverable.length).toBe(1);
			expect(recoverable[0].id).toBe("uc-planning");
		});

		it("does not recover cancelled tasks", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			await store.save(makeTask({ id: "uc-cancelled", status: "cancelled", controlState: "cancelled" }));

			const recoverable = await store.loadRecoverable();
			expect(recoverable.length).toBe(0);
		});

		it("handles empty directory gracefully", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			expect(await store.loadAll()).toEqual([]);
			expect(await store.loadRecoverable()).toEqual([]);
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

		// ── resumeFromWave round-trip ──────────────────────────────────

		it("persists and restores resumeFromWave", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			const task = makeTask({
				id: "uc-wave-persist",
				controlState: "paused",
				resumeFromWave: 3,
			});
			await store.save(task);

			const loaded = await store.load("uc-wave-persist");
			expect(loaded!.resumeFromWave).toBe(3);
			expect(loaded!.controlState).toBe("paused");
		});

		it("resumeFromWave undefined when not set", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			const task = makeTask({ id: "uc-no-wave" });
			await store.save(task);

			const loaded = await store.load("uc-no-wave");
			expect(loaded!.resumeFromWave).toBeUndefined();
		});

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

		// ── Checkpoint save/load ───────────────────────────────────────

		it("saves and loads a checkpoint", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			const task = makeTask({
				id: "uc-cp-test",
				controlState: "paused",
				resumeFromWave: 2,
			});
			await store.saveCheckpoint(task);

			const cp = await store.loadCheckpoint("uc-cp-test");
			expect(cp).not.toBeNull();
			expect(cp!.id).toBe("uc-cp-test");
			expect(cp!.resumeFromWave).toBe(2);
			expect(cp!.controlState).toBe("paused");
		});

		it("returns null for nonexistent checkpoint", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			expect(await store.loadCheckpoint("no-such-task")).toBeNull();
		});

		it("checkpoint overwrites on second save (latest-wins)", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			await store.saveCheckpoint(makeTask({ id: "uc-cp-ow", resumeFromWave: 1 }));
			await store.saveCheckpoint(makeTask({ id: "uc-cp-ow", resumeFromWave: 4 }));

			const cp = await store.loadCheckpoint("uc-cp-ow");
			expect(cp!.resumeFromWave).toBe(4);
		});

		it("checkpoint is independent from task file", async () => {
			const store = new TaskStore(testDir);
			await store.init();

			const task = makeTask({ id: "uc-cp-indep", resumeFromWave: 2 });
			await store.save(task);
			await store.saveCheckpoint(task);

			// Update task file — checkpoint should still have old value
			await store.save(makeTask({ id: "uc-cp-indep", resumeFromWave: 5 }));
			const cp = await store.loadCheckpoint("uc-cp-indep");
			expect(cp!.resumeFromWave).toBe(2);
		});
});

// ── File-Aware Wave Splitting tests ────────────────────────────────

describe("splitWavesByFileOverlap", () => {
	it("no-op when all subtasks have no files", () => {
		const waves = buildDAG([st("a", "a"), st("b", "b"), st("c", "c")]);
		const split = splitWavesByFileOverlap(waves);
		expect(split.length).toBe(1);
		expect(split[0].length).toBe(3);
	});

	it("no-op when no file overlap", () => {
		const waves = buildDAG([
			st("a", "a", [], ["file1.ts"]),
			st("b", "b", [], ["file2.ts"]),
			st("c", "c", [], ["file3.ts"]),
		]);
		const split = splitWavesByFileOverlap(waves);
		expect(split.length).toBe(1);
		expect(split[0].length).toBe(3);
	});

	it("splits wave when two subtasks share a file", () => {
		const waves = buildDAG([
			st("a", "a", [], ["file1.ts", "file2.ts"]),
			st("b", "b", [], ["file2.ts", "file3.ts"]),
			st("c", "c", [], ["file4.ts"]),
		]);
		const split = splitWavesByFileOverlap(waves);
		// a and b conflict, c is independent → 2 sub-waves
		expect(split.length).toBe(2);
		const sizes = split.map((w) => w.length).sort();
		expect(sizes).toEqual([1, 2]);
	});

	it("fully sequential when all subtasks share a file", () => {
		const waves = buildDAG([
			st("a", "a", [], ["shared.ts"]),
			st("b", "b", [], ["shared.ts"]),
			st("c", "c", [], ["shared.ts"]),
		]);
		const split = splitWavesByFileOverlap(waves);
		expect(split.length).toBe(3);
		for (const w of split) {
			expect(w.length).toBe(1);
		}
	});

	it("subtasks with empty files never conflict", () => {
		const waves = buildDAG([
			st("a", "a", [], ["file1.ts"]),
			st("b", "b", [], []),
			st("c", "c", [], ["file1.ts"]),
		]);
		const split = splitWavesByFileOverlap(waves);
		expect(split.length).toBe(2);
	});

	it("preserves dependency ordering across waves", () => {
		const waves = buildDAG([
			st("a", "a"),
			st("b", "b", ["a"]),
		]);
		const split = splitWavesByFileOverlap(waves);
		expect(split.length).toBe(2);
		expect(split[0][0].id).toBe("a");
		expect(split[1][0].id).toBe("b");
	});

	it("handles multi-wave input where only middle wave needs splitting", () => {
		// Wave 1: [a] (no split needed), Wave 2: [b, c] (share file, needs split), Wave 3: [d]
		const waves = buildDAG([
			st("a", "a", [], ["file1.ts"]),
			st("b", "b", ["a"], ["file2.ts"]),
			st("c", "c", ["a"], ["file2.ts"]),
			st("d", "d", ["b", "c"]),
		]);
		const split = splitWavesByFileOverlap(waves);
		// Wave 1 [a] unchanged, Wave 2 [b,c] split into 2, Wave 3 [d] unchanged = 4 sub-waves
		expect(split.length).toBe(4);
		expect(split[0].map((s) => s.id)).toEqual(["a"]);
		expect(split[3].map((s) => s.id)).toEqual(["d"]);
	});

	it("partial overlap causes split", () => {
		const waves = buildDAG([
			st("a", "a", [], ["x.ts"]),
			st("b", "b", [], ["y.ts"]),
			st("c", "c", [], ["x.ts", "y.ts"]),
		]);
		const split = splitWavesByFileOverlap(waves);
		// c conflicts with both a and b; a and b don't conflict with each other
		// Greedy coloring: a=0, b=0, c=1 -> 2 sub-waves: [a,b] and [c]
		expect(split.length).toBe(2);
		expect(split[0].length).toBe(2);
		expect(split[1].length).toBe(1);
	});
});

// ── FileIntentTracker tests ────────────────────────────────────────

describe("FileIntentTracker", () => {
	it("declares and releases intents", () => {
		const tracker = new FileIntentTracker();
		tracker.declare("st-1", ["a.ts", "b.ts"]);
		expect(tracker.isConflicting(["a.ts"]).size).toBe(1);
		tracker.release("st-1");
		expect(tracker.isConflicting(["a.ts"]).size).toBe(0);
	});

	it("detects conflict across multiple subtasks", () => {
		const tracker = new FileIntentTracker();
		tracker.declare("st-1", ["a.ts"]);
		tracker.declare("st-2", ["b.ts"]);
		expect(tracker.isConflicting(["a.ts"])).toEqual(new Set(["st-1"]));
		expect(tracker.isConflicting(["b.ts"])).toEqual(new Set(["st-2"]));
		expect(tracker.isConflicting(["c.ts"]).size).toBe(0);
	});

	it("no conflict for empty files", () => {
		const tracker = new FileIntentTracker();
		tracker.declare("st-1", ["a.ts"]);
		expect(tracker.isConflicting([]).size).toBe(0);
	});

	it("release is idempotent", () => {
		const tracker = new FileIntentTracker();
		tracker.declare("st-1", ["a.ts"]);
		tracker.release("st-1");
		tracker.release("st-1");
		expect(tracker.isConflicting(["a.ts"]).size).toBe(0);
	});

	it("getOwnedFiles returns correct map", () => {
		const tracker = new FileIntentTracker();
		tracker.declare("st-1", ["a.ts", "b.ts"]);
		tracker.declare("st-2", ["a.ts"]);
		const owned = tracker.getOwnedFiles();
		expect(owned.get("a.ts")!.sort()).toEqual(["st-1", "st-2"]);
		expect(owned.get("b.ts")).toEqual(["st-1"]);
	});

	it("clear removes all intents", () => {
		const tracker = new FileIntentTracker();
		tracker.declare("st-1", ["a.ts"]);
		tracker.clear();
		expect(tracker.isConflicting(["a.ts"]).size).toBe(0);
		expect(tracker.getOwnedFiles().size).toBe(0);
	});

	it("re-declare releases old intents first", () => {
		const tracker = new FileIntentTracker();
		tracker.declare("st-1", ["a.ts", "b.ts"]);
		// Re-declare with different files — old intents should be released
		tracker.declare("st-1", ["c.ts"]);
		expect(tracker.isConflicting(["a.ts"]).size).toBe(0);
		expect(tracker.isConflicting(["b.ts"]).size).toBe(0);
		expect(tracker.isConflicting(["c.ts"]).size).toBe(1);
		const owned = tracker.getOwnedFiles();
		expect(owned.has("a.ts")).toBe(false);
		expect(owned.has("b.ts")).toBe(false);
		expect(owned.get("c.ts")).toEqual(["st-1"]);
	});

	it("same file owned by multiple subtasks", () => {
		const tracker = new FileIntentTracker();
		tracker.declare("st-1", ["shared.ts"]);
		tracker.declare("st-2", ["shared.ts"]);
		const conflicting = tracker.isConflicting(["shared.ts"]);
		expect(conflicting).toEqual(new Set(["st-1", "st-2"]));
		// Releasing one should still leave the other
		tracker.release("st-1");
		expect(tracker.isConflicting(["shared.ts"])).toEqual(new Set(["st-2"]));
	});
});
