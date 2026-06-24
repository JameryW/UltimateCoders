/**
 * Scheduler + TaskStore self-check — validates DAG construction and persistence logic.
 *
 * Run: bun test src/orchestrator/scheduler.test.ts
 */

import { beforeEach, describe, expect, it } from "bun:test";
import { buildDAG, detectCycles, type SubtaskDef } from "./scheduler";
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
				{ id: "st-1", description: "subtask 1", status: "completed", dependsOn: [] },
				{ id: "st-2", description: "subtask 2", status: "pending", dependsOn: ["st-1"] },
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
});
