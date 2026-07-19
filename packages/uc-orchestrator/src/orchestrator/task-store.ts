/**
 * TaskStore — Local JSON file persistence for task state.
 *
 * ponytail: flat JSON files in .uc/tasks/, one per task.
 * Upgrade to SQLite if concurrent writers become a concern.
 */

import { promises as fs } from "node:fs";
import * as path from "node:path";
import type { WorkflowStepDef, DispatchMode } from "./scheduler";

// ── Types ──────────────────────────────────────────────────────────

/** Subset of TaskState that gets persisted. */
export interface PersistedTask {
	id: string;
	description: string;
	status: string;
	error?: string;
	controlState: "running" | "paused" | "cancelled";
	/** Which wave to resume from (persisted — fixes resume-from-wave-0 bug). */
	resumeFromWave?: number;
	/** Whether re-decomposition has been attempted (one-shot guard, persisted). */
	redecomposed?: boolean;
	/** Project scope for cross-repo search and memory sharing. */
	projectId?: string;
	subtasks: Array<{
		id: string;
		description: string;
		status: string;
		dependsOn: string[];
		/** Declared file intents from SubtaskDef.files (for resume conflict detection). */
		files: string[];
		result?: string;
		error?: string;
		review?: { approved: boolean; issues: string[]; suggestions: string[] };
		startedAt?: number;
		completedAt?: number;
		modifiedFiles?: string[];
		recentToolCalls?: string[];
		stderrTail?: string;
		retryCount?: number;
		/** Dispatch mode: "local" | "remote" | "prefer_remote" | "auto" */
		dispatchMode?: DispatchMode;
		/** Capabilities required by this subtask (e.g. "rust", "python"). Worker must have ALL. */
		requiredCapabilities?: string[];
		/** Ordered multi-agent workflow steps. Empty/undefined = single-agent (backward compatible). */
		steps?: WorkflowStepDef[];
	}>;
	createdAt: number;
	completedAt?: number;
	/**
	 * Write timestamp stamped by save()/saveCheckpoint() (F46). restore()
	 * prefers the NEWER of task file vs checkpoint — checkpoints are only
	 * written at wave boundaries, so an unconditional checkpoint preference
	 * rolled mid-wave progress back after a crash. Absent on legacy files.
	 */
	savedAt?: number;
}

// ── TaskStore ──────────────────────────────────────────────────────

export class TaskStore {
	private dir: string;
	private checkpointDir: string;

	constructor(cwd: string) {
		this.dir = path.join(cwd, ".uc", "tasks");
		this.checkpointDir = path.join(cwd, ".uc", "checkpoints");
	}

	async init(): Promise<void> {
		await fs.mkdir(this.dir, { recursive: true });
		await fs.mkdir(this.checkpointDir, { recursive: true });
	}

	async save(task: PersistedTask): Promise<void> {
		// ponytail: F42 — atomic write. Direct writeFile leaves a truncated/empty
		// file when the process dies mid-write (SIGKILL/OOM — the RPC server is
		// killed by its parent), which loadAll then can't parse. tmp + rename is
		// atomic on POSIX, so readers see either the old or the new file.
		const filePath = path.join(this.dir, `${task.id}.json`);
		// ponytail: F46 — stamp savedAt on the WRITTEN copy (don't mutate the
		// caller's in-memory object).
		const stamped = { ...task, savedAt: Date.now() };
		await fs.writeFile(`${filePath}.tmp`, JSON.stringify(stamped, null, 2), "utf-8");
		await fs.rename(`${filePath}.tmp`, filePath);
	}

	async load(taskId: string): Promise<PersistedTask | null> {
		try {
			const filePath = path.join(this.dir, `${taskId}.json`);
			const raw = await fs.readFile(filePath, "utf-8");
			return JSON.parse(raw) as PersistedTask;
		} catch (err) {
			if (!(err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT")) {
				console.warn(`TaskStore load failed for ${taskId}: ${err instanceof Error ? err.message : err}`);
			}
			return null;
		}
	}

	async loadAll(): Promise<PersistedTask[]> {
		let files: string[];
		try {
			files = await fs.readdir(this.dir);
		} catch (err) {
			if (!(err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT")) {
				console.warn(`TaskStore loadAll failed: ${err instanceof Error ? err.message : err}`);
			}
			return [];
		}
		const tasks: PersistedTask[] = [];
		for (const file of files) {
			if (!file.endsWith(".json")) continue;
			// ponytail: F41 — per-file try/catch. The old single catch around the
			// whole loop meant ONE corrupt file discarded every task (restore()
			// showed nothing until the bad file was found by hand). Skip the bad
			// file, keep the rest.
			try {
				const raw = await fs.readFile(path.join(this.dir, file), "utf-8");
				tasks.push(JSON.parse(raw) as PersistedTask);
			} catch (err) {
				console.warn(`TaskStore skipping unreadable task file ${file}: ${err instanceof Error ? err.message : err}`);
			}
		}
		return tasks;
	}

	async remove(taskId: string): Promise<void> {
		try {
			await fs.unlink(path.join(this.dir, `${taskId}.json`));
		} catch (err) {
			if (!(err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT")) {
				console.warn(`TaskStore remove failed for ${taskId}: ${err instanceof Error ? err.message : err}`);
			}
		}
	}

	/** Load tasks that are recoverable (not completed/cancelled). */
	async loadRecoverable(): Promise<PersistedTask[]> {
		const all = await this.loadAll();
		return all.filter(
			(t) => t.status === "planning" || t.status === "in_progress" || t.status === "failed" || t.controlState === "paused",
		);
	}

	/** Save a wave-boundary checkpoint snapshot (latest-wins). */
	async saveCheckpoint(task: PersistedTask): Promise<void> {
		// ponytail: F42 — atomic write, same rationale as save().
		// F46 — savedAt stamp so restore() can pick the newer artifact.
		const filePath = path.join(this.checkpointDir, `${task.id}.snap.json`);
		const stamped = { ...task, savedAt: Date.now() };
		await fs.writeFile(`${filePath}.tmp`, JSON.stringify(stamped, null, 2), "utf-8");
		await fs.rename(`${filePath}.tmp`, filePath);
	}

	/** Load the latest checkpoint for a task, or null if none exists. */
	async loadCheckpoint(taskId: string): Promise<PersistedTask | null> {
		try {
			const filePath = path.join(this.checkpointDir, `${taskId}.snap.json`);
			const raw = await fs.readFile(filePath, "utf-8");
			return JSON.parse(raw) as PersistedTask;
		} catch (err) {
			if (!(err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT")) {
				console.warn(`TaskStore loadCheckpoint failed for ${taskId}: ${err instanceof Error ? err.message : err}`);
			}
			return null;
		}
	}

	/** Remove task files and checkpoint files not in the keep set. */
	async removeStale(taskIdsToKeep: Set<string>): Promise<number> {
		let removed = 0;
		for (const dir of [this.dir, this.checkpointDir]) {
			try {
				const files = await fs.readdir(dir);
				for (const file of files) {
					// ponytail: F42 — also sweep .tmp orphans a crash can leave
					// behind from the atomic-write rename.
					const isTmp = file.endsWith(".json.tmp");
					if (!file.endsWith(".json") && !isTmp) continue;
					const taskId = file.replace(/\.json\.tmp$|\.snap\.json$|\.json$/, "");
					if (!taskIdsToKeep.has(taskId)) {
						try {
							await fs.unlink(path.join(dir, file));
							removed++;
						} catch (err) {
							if (!(err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT")) {
								console.warn(`TaskStore removeStale unlink failed for ${file}: ${err instanceof Error ? err.message : err}`);
							}
						}
					}
				}
			} catch (err) {
				if (!(err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT")) {
					console.warn(`TaskStore removeStale readdir failed: ${err instanceof Error ? err.message : err}`);
				}
			}
		}
		return removed;
	}
}
