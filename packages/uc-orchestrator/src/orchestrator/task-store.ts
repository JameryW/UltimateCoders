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
	subtasks: Array<{
		id: string;
		description: string;
		status: string;
		dependsOn: string[];
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

	constructor(cwd: string) {
		this.dir = path.join(cwd, ".uc", "tasks");
	}

	async init(): Promise<void> {
		await fs.mkdir(this.dir, { recursive: true });
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
}
