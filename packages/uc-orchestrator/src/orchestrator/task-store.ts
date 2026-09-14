/**
 * TaskStore — Local JSON file persistence for task state.
 *
 * ponytail: flat JSON files in .uc/tasks/, one per task.
 * Upgrade to SQLite if concurrent writers become a concern.
 */

import { promises as fs } from "node:fs";
import * as path from "node:path";
import type { WorkflowStepDef, DispatchMode, ConflictRisk } from "./scheduler";

// ── Types ──────────────────────────────────────────────────────────

/** Subset of TaskState that gets persisted. */
export interface PersistedTask {
	id: string;
	description: string;
	status: string;
	error?: string;
	controlState: "running" | "paused" | "cancelled";
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
		/** File-overlap parallelism grade (C5) — rides the cache so the claim
		 *  gate survives a restore. Never sent to the server. */
		conflictRisk?: ConflictRisk;
		/** Capabilities required by this subtask (e.g. "rust", "python"). Worker must have ALL. */
		requiredCapabilities?: string[];
		/** Ordered multi-agent workflow steps. Empty/undefined = single-agent (backward compatible). */
		steps?: WorkflowStepDef[];
	}>;
	createdAt: number;
	completedAt?: number;
	/**
	 * Write timestamp stamped by save(). Absent on legacy files — the UI
	 * cache-refresh path uses it to reason about projection freshness.
	 */
	savedAt?: number;
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

	/**
	 * ponytail: F51 — defense in depth. Every method below builds file paths
	 * from task ids; ids are currently server-generated ("uc-<n>-<ts>") and
	 * never come from external input, so this is unreachable today — but if an
	 * untrusted id path ever appears, "../" must not escape the tasks dir.
	 */
	private assertSafeId(taskId: string): void {
		if (!/^[\w.-]+$/.test(taskId) || taskId.includes("..")) {
			throw new Error(`Unsafe task id rejected: ${JSON.stringify(taskId)}`);
		}
	}

	/** Monotonic tmp-suffix counter — see save(). */
	private tmpSeq = 0;

	async save(task: PersistedTask): Promise<void> {
		this.assertSafeId(task.id);
		// ponytail: F42 — atomic write. Direct writeFile leaves a truncated/empty
		// file when the process dies mid-write (SIGKILL/OOM — the RPC server is
		// killed by its parent), which loadAll then can't parse. tmp + rename is
		// atomic on POSIX, so readers see either the old or the new file.
		const filePath = path.join(this.dir, `${task.id}.json`);
		// ponytail: F46 — stamp savedAt on the WRITTEN copy (don't mutate the
		// caller's in-memory object).
		const stamped = { ...task, savedAt: Date.now() };
		// T6 #642 C7 — unique tmp name per call: a task-level cancel can race
		// an in-flight runClaimed's outcome persist (two concurrent saves of
		// the SAME task file); a shared `${filePath}.tmp` made the second
		// rename fail with ENOENT (first rename consumed it). Per-call suffix
		// keeps each write→rename pair self-contained; last rename wins, which
		// is fine — both writes carry equivalent state snapshots.
		const tmpPath = `${filePath}.${++this.tmpSeq}-${Date.now().toString(36)}.tmp`;
		await fs.writeFile(tmpPath, JSON.stringify(stamped, null, 2), "utf-8");
		await fs.rename(tmpPath, filePath);
	}

	async load(taskId: string): Promise<PersistedTask | null> {
		this.assertSafeId(taskId);
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
		this.assertSafeId(taskId);
		try {
			await fs.unlink(path.join(this.dir, `${taskId}.json`));
		} catch (err) {
			if (!(err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT")) {
				console.warn(`TaskStore remove failed for ${taskId}: ${err instanceof Error ? err.message : err}`);
			}
		}
	}

	/**
	 * T6 #642 C4 — wave-boundary checkpoints retired: the Rust gateway is the
	 * execution authority (crash recovery = graph-plane committed-state +
	 * T2's startup import), so `.uc/checkpoints` is gone. This file remains
	 * a pure UI projection cache (C3).
	 *
	 * Remove task files not in the keep set.
	 */
	async removeStale(taskIdsToKeep: Set<string>): Promise<number> {
		let removed = 0;
		{
			try {
				const files = await fs.readdir(this.dir);
				for (const file of files) {
					// ponytail: F42 — also sweep .tmp orphans a crash can leave
					// behind from the atomic-write rename.
					const isTmp = file.endsWith(".json.tmp");
					if (!file.endsWith(".json") && !isTmp) continue;
					const taskId = file.replace(/\.json\.tmp$|\.snap\.json$|\.json$/, "");
					if (!taskIdsToKeep.has(taskId)) {
						try {
							await fs.unlink(path.join(this.dir, file));
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
