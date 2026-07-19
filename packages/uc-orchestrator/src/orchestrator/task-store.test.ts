/**
 * TaskStore persistence robustness tests.
 *
 * F41: one corrupt task file must not discard every other task on loadAll.
 * F42: writes are atomic (tmp + rename) so a mid-write crash can't leave an
 * unparseable file; removeStale sweeps leftover .tmp orphans.
 *
 * Run: bun test src/orchestrator/task-store.test.ts
 */

import { describe, expect, it, beforeEach, afterEach } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { TaskStore, type PersistedTask } from "./task-store";

function makeTask(id: string, status = "in_progress"): PersistedTask {
	return {
		id, description: `task ${id}`, status, controlState: "running",
		subtasks: [], createdAt: Date.now(),
	};
}

describe("TaskStore", () => {
	let ws: string;
	let store: TaskStore;

	beforeEach(async () => {
		ws = mkdtempSync(join(tmpdir(), "uc-taskstore-"));
		store = new TaskStore(ws);
		await store.init();
	});

	afterEach(() => {
		rmSync(ws, { recursive: true, force: true });
	});

	it("F41: one corrupt file no longer discards all tasks", async () => {
		await store.save(makeTask("good-1"));
		await store.save(makeTask("good-2"));
		writeFileSync(join(ws, ".uc", "tasks", "bad.json"), "{ not json !!!");
		const all = await store.loadAll();
		expect(all.map((t) => t.id).sort()).toEqual(["good-1", "good-2"]);
	});

	it("F42: save leaves no .tmp residue and content round-trips", async () => {
		await store.save(makeTask("t-1"));
		const files = readdirSync(join(ws, ".uc", "tasks"));
		expect(files).toContain("t-1.json");
		expect(files.some((f) => f.endsWith(".tmp"))).toBe(false);
		const loaded = await store.load("t-1");
		expect(loaded?.id).toBe("t-1");
		expect(loaded?.description).toBe("task t-1");
	});

	it("F42: saveCheckpoint is atomic too", async () => {
		await store.saveCheckpoint(makeTask("t-2"));
		const files = readdirSync(join(ws, ".uc", "checkpoints"));
		expect(files).toContain("t-2.snap.json");
		expect(files.some((f) => f.endsWith(".tmp"))).toBe(false);
		const loaded = await store.loadCheckpoint("t-2");
		expect(loaded?.id).toBe("t-2");
	});

	it("F42: removeStale sweeps .tmp orphans a crash can leave", async () => {
		await store.save(makeTask("keep"));
		writeFileSync(join(ws, ".uc", "tasks", "ghost.json.tmp"), "orphan");
		const removed = await store.removeStale(new Set(["keep"]));
		expect(removed).toBe(1);
		expect(readdirSync(join(ws, ".uc", "tasks"))).toEqual(["keep.json"]);
	});
});
