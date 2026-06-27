/**
 * TaskStore — Local JSON file persistence for task state.
 *
 * ponytail: flat JSON files in .uc/tasks/, one per task.
 * Upgrade to SQLite if concurrent writers become a concern.
 */

import { promises as fs } from "node:fs";
import * as path from "node:path";

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
	}>;
	createdAt: number;
	completedAt?: number;
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
		const filePath = path.join(this.dir, `${task.id}.json`);
		await fs.writeFile(filePath, JSON.stringify(task, null, 2), "utf-8");
	}

	async load(taskId: string): Promise<PersistedTask | null> {
		try {
			const filePath = path.join(this.dir, `${taskId}.json`);
			const raw = await fs.readFile(filePath, "utf-8");
			return JSON.parse(raw) as PersistedTask;
		} catch {
			return null;
		}
	}

	async loadAll(): Promise<PersistedTask[]> {
		try {
			const files = await fs.readdir(this.dir);
			const tasks: PersistedTask[] = [];
			for (const file of files) {
				if (!file.endsWith(".json")) continue;
				const raw = await fs.readFile(path.join(this.dir, file), "utf-8");
				tasks.push(JSON.parse(raw) as PersistedTask);
			}
			return tasks;
		} catch {
			return [];
		}
	}

	async remove(taskId: string): Promise<void> {
		try {
			await fs.unlink(path.join(this.dir, `${taskId}.json`));
		} catch {
			// already removed is fine
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
		const filePath = path.join(this.checkpointDir, `${task.id}.snap.json`);
		await fs.writeFile(filePath, JSON.stringify(task, null, 2), "utf-8");
	}

	/** Load the latest checkpoint for a task, or null if none exists. */
	async loadCheckpoint(taskId: string): Promise<PersistedTask | null> {
		try {
			const filePath = path.join(this.checkpointDir, `${taskId}.snap.json`);
			const raw = await fs.readFile(filePath, "utf-8");
			return JSON.parse(raw) as PersistedTask;
		} catch {
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
					if (!file.endsWith(".json")) continue;
					const taskId = file.replace(/\.snap\.json$|\.json$/, "");
					if (!taskIdsToKeep.has(taskId)) {
						try {
							await fs.unlink(path.join(dir, file));
							removed++;
						} catch {
							// already removed is fine
						}
					}
				}
			} catch {
				// directory doesn't exist yet — fine
			}
		}
		return removed;
	}
}
