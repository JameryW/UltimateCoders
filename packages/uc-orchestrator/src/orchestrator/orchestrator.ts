/**
 * UCOrchestrator — Core task orchestration logic.
 *
 * Manages the lifecycle of a task:
 * 1. Decompose task into subtasks via omp decomposer agent
 * 2. Build DAG from subtask dependencies
 * 3. Execute waves of subtasks via omp runSubprocess
 * 4. Optionally review subtask results via supervisor agent
 * 5. Collect results and inject summary into conversation
 * 6. Persist state to local JSON + sync to UC gRPC TaskStore
 *
 * Supports: cancel/pause/resume, subtask-level control,
 * context injection from completed subtasks.
 */

import type { ExtensionAPI, ExtensionCommandContext } from "@oh-my-pi/pi-coding-agent";
import { runSubprocess } from "@oh-my-pi/pi-coding-agent";
import { buildDAG, splitWavesByFileOverlap, FileIntentTracker, CircuitBreaker, type SubtaskDef } from "./scheduler";
import { GrpcBridge } from "./grpc-bridge";
import { TaskStore, type PersistedTask } from "./task-store";
import { ControlSignalSubscriber, type ControlSignalHandler } from "./control-signal-subscriber";
import { OrchestratorEventEmitter } from "./events";
import type { OrchestratorEvents } from "./events";

// ── Types ──────────────────────────────────────────────────────────

type ControlState = "running" | "paused" | "cancelled";

// ponytail: heuristic extractors — parse agent output for checkpoint fields
/** Extract file paths mentioned in tool_use / Edit / Write blocks from agent output. */
function extractModifiedFiles(output: string): string[] {
	const files: string[] = [];
	// Match common patterns: file_path, file_path in tool calls
	for (const m of output.matchAll(/(?:file_path|path|file):\s*["']([^"']+)["']/g)) {
		if (m[1] && !files.includes(m[1])) files.push(m[1]);
	}
	return files.slice(0, 20);
}

/** Extract last N tool call names from agent output. */
function extractRecentToolCalls(output: string, n: number): string[] {
	const calls: string[] = [];
	for (const m of output.matchAll(/"type"\s*:\s*"tool_use"[^}]*"name"\s*:\s*"([^"]+)"/g)) {
		calls.push(m[1]);
	}
	// Fallback: match tool_use lines in text
	if (calls.length === 0) {
		for (const m of output.matchAll(/Using tool:\s*(\w+)/g)) {
			calls.push(m[1]);
		}
	}
	return calls.slice(-n);
}

export interface TaskState {
	id: string;
	description: string;
	status: "planning" | "in_progress" | "completed" | "failed" | "cancelled";
	controlState: ControlState;
	subtasks: SubtaskResult[];
	createdAt: number;
	completedAt?: number;
	error?: string;
	/** Which wave to resume from (for pause/resume). */
	resumeFromWave?: number;
	/** Whether re-decomposition has been attempted (one-shot guard). */
	redecomposed?: boolean;
}

export interface SubtaskResult {
	id: string;
	description: string;
	status: "pending" | "running" | "reviewing" | "completed" | "failed" | "cancelled";
	dependsOn: string[];
	/** Declared file intents from SubtaskDef.files (for resume conflict detection). */
	files: string[];
	result?: string;
	error?: string;
	review?: ReviewResult;
	startedAt?: number;
	completedAt?: number;
	/** Files modified by this subtask (populated on completion). */
	modifiedFiles?: string[];
	/** Recent tool calls (last 5, for checkpoint + debugging). */
	recentToolCalls?: string[];
	/** Last lines of stderr (for failure context). */
	stderrTail?: string;
	/** How many retries this subtask has used (for checkpoint). */
	retryCount?: number;
}

interface ReviewResult {
	approved: boolean;
	issues: string[];
	suggestions: string[];
}

interface OrchestratorConfig {
	enableReview: boolean;
	reviewTimeoutMs: number;
	maxConcurrency: number;
	maxRetries: number;
	retryBaseDelayMs: number;
}

// ── Orchestrator ───────────────────────────────────────────────────

export class UCOrchestrator {
	private pi: ExtensionAPI;
	private tasks: Map<string, TaskState> = new Map();
	private abortControllers: Map<string, AbortController> = new Map();
	private taskCounter = 0;
	private config: OrchestratorConfig;
	private bridge: GrpcBridge;
	private store: TaskStore;
	private runningCount = 0;
	private circuitBreaker = new CircuitBreaker();
	private controlSubscriber: ControlSignalSubscriber;
	/** Internal event emitter — decouples orchestration from presentation */
	readonly events = new OrchestratorEventEmitter();

	constructor(pi: ExtensionAPI, config?: Partial<OrchestratorConfig>, bridge?: GrpcBridge) {
		this.pi = pi;
		this.config = {
			enableReview: true,
			reviewTimeoutMs: 60_000,
			maxConcurrency: 3,
			maxRetries: 2,
			retryBaseDelayMs: 5_000,
			...config,
		};
		this.bridge = bridge ?? new GrpcBridge();
		// ponytail: workspaceRoot may not exist on Settings — fallback to cwd
		const settings = pi.pi.settings as unknown as Record<string, unknown>;
		const ws = settings.workspaceRoot;
		this.store = new TaskStore(typeof ws === "string" ? ws : process.cwd());
		// ponytail: subscribe to NATS control events (pause/resume/cancel from TUI/Dashboard)
		this.controlSubscriber = new ControlSignalSubscriber(this as ControlSignalHandler);
	}

	/** Restore recoverable tasks from disk. Call once at startup. */
	async restore(): Promise<void> {
		await this.store.init();
		const recoverable = await this.store.loadRecoverable();
		for (const p of recoverable) {
			// Prefer checkpoint data (has accurate resumeFromWave)
			const cp = await this.store.loadCheckpoint(p.id);
			const source = cp ?? p;
			const task = this.fromPersisted(source);
			this.tasks.set(task.id, task);
			// Update counter to avoid ID collision
			const counterPart = task.id.match(/^uc-(\d+)-/)?.[1];
			if (counterPart) {
				const n = parseInt(counterPart, 10);
				if (n > this.taskCounter) this.taskCounter = n;
			}
		}
		if (recoverable.length > 0) {
			this.pi.logger.info(`Restored ${recoverable.length} task(s) from disk`);
		}
		// Start NATS control subscriber (or polling fallback) — non-blocking
		this.controlSubscriber.start().catch((err) => {
			this.pi.logger.warn(`ControlSubscriber start failed: ${err}`);
		});
	}

	// ── Task Submission ──────────────────────────────────────────────

	async submitTask(description: string, ctx?: ExtensionCommandContext): Promise<string> {
		this.circuitBreaker.reset();
		const taskId = `uc-${++this.taskCounter}-${Date.now().toString(36)}`;

		const task: TaskState = {
			id: taskId,
			description,
			status: "planning",
			controlState: "running",
			subtasks: [],
			createdAt: Date.now(),
		};
		this.tasks.set(taskId, task);
		this.abortControllers.set(taskId, new AbortController());

		ctx?.ui.notify(`Task ${taskId}: planning...`, "info");
		this.events.emit("task_planning", { taskId, description });
		await this.persist(task);

		// ponytail: stub ctx for rpc server (no omp context)
		const execCtx = ctx ?? stubContext();

		// ── Step 1: Decompose ──
		let subtaskDefs: SubtaskDef[];
		try {
			subtaskDefs = await this.decompose(description, execCtx);
		} catch (err) {
			task.status = "failed";
			task.error = `Decomposition failed: ${err instanceof Error ? err.message : String(err)}`;
			execCtx.ui.notify(`Task ${taskId} failed: ${task.error}`, "error");
			await this.persist(task);
			this.syncTaskToGrpc(task);
			return taskId;
		}

		// ── Step 2: Build DAG ──
		const waves = splitWavesByFileOverlap(buildDAG(subtaskDefs));

		task.subtasks = subtaskDefs.map((def) => ({
			id: def.id,
			description: def.description,
			status: "pending",
			dependsOn: def.dependsOn,
			files: def.files,
		}));
		task.status = "in_progress";
		await this.persist(task);
		this.syncTaskToGrpc(task);

		execCtx.ui.notify(
			`Task ${taskId}: ${subtaskDefs.length} subtasks, ${waves.length} wave(s)`,
			"info",
		);
		this.events.emit("task_decomposed", { taskId, subtaskCount: subtaskDefs.length, waveCount: waves.length });

		// ── Step 3: Execute waves ──
		await this.executeWaves(task, waves, execCtx);
		return taskId;
	}

	/**
	 * Create a task entry synchronously and return its ID.
	 * Used by the RPC server to get an immediate task_id before
	 * the async decomposition + execution begins.
	 */
	createTask(description: string): string {
		this.circuitBreaker.reset();
		const taskId = `uc-${++this.taskCounter}-${Date.now().toString(36)}`;
		const task: TaskState = {
			id: taskId,
			description,
			status: "planning",
			controlState: "running",
			subtasks: [],
			createdAt: Date.now(),
		};
		this.tasks.set(taskId, task);
		this.abortControllers.set(taskId, new AbortController());
		this.persist(task).catch(() => {});
		return taskId;
	}

	/**
	 * Run the full lifecycle (decompose + execute) for a task created by createTask().
	 * Designed to be called fire-and-forget from the RPC server.
	 */
	async runTask(taskId: string, ctx?: ExtensionCommandContext): Promise<void> {
		const task = this.tasks.get(taskId);
		if (!task) throw new Error(`Task ${taskId} not found`);

		const execCtx = ctx ?? stubContext();
		execCtx.ui.notify(`Task ${taskId}: planning...`, "info");

		// Step 1: Decompose
		let subtaskDefs: SubtaskDef[];
		try {
			subtaskDefs = await this.decompose(task.description, execCtx);
		} catch (err) {
			task.status = "failed";
			task.error = `Decomposition failed: ${err instanceof Error ? err.message : String(err)}`;
			execCtx.ui.notify(`Task ${taskId} failed: ${task.error}`, "error");
			await this.persist(task);
			this.syncTaskToGrpc(task);
			return;
		}

		// Step 2: Build DAG
		const waves = splitWavesByFileOverlap(buildDAG(subtaskDefs));
		task.subtasks = subtaskDefs.map((def) => ({
			id: def.id,
			description: def.description,
			status: "pending",
			dependsOn: def.dependsOn,
			files: def.files,
		}));
		task.status = "in_progress";
		await this.persist(task);
		this.syncTaskToGrpc(task);

		execCtx.ui.notify(
			`Task ${taskId}: ${subtaskDefs.length} subtasks, ${waves.length} wave(s)`,
			"info",
		);
		this.events.emit("task_decomposed", { taskId, subtaskCount: subtaskDefs.length, waveCount: waves.length });

		// Step 3: Execute waves
		await this.executeWaves(task, waves, execCtx);
	}

	// ── Worker Availability Check ────────────────────────────────────

	/**
	 * Check if any worker is available via gRPC bridge.
	 * Returns true if at least one worker is available, false otherwise.
	 * In degraded mode (Health RPC fallback), treats local_worker as available.
	 */
	private async checkWorkerAvailability(): Promise<boolean> {
		try {
			const result = await this.bridge.listWorkers();
			if (!result.available) return false;
			return result.availableCount > 0;
		} catch {
			return false;
		}
	}

	// ── Wave Execution ───────────────────────────────────────────────

	private async executeWaves(
		task: TaskState,
		waves: SubtaskDef[][],
		ctx: ExtensionCommandContext,
	): Promise<void> {
		const widgetKey = `uc-${task.id}`;
		const startWave = task.resumeFromWave ?? 0;
		this.updateWidget(ctx, widgetKey, task);

		try {
			for (let waveIdx = startWave; waveIdx < waves.length; waveIdx++) {
				// Check control state before each wave
				if (task.controlState === "cancelled") {
					task.status = "cancelled";
					await this.persist(task);
					break;
				}
				if (task.controlState === "paused") {
					task.resumeFromWave = waveIdx;
					await this.persist(task);
					this.updateWidget(ctx, widgetKey, task);
					ctx.ui.notify(`Task ${task.id}: paused at wave ${waveIdx + 1}`, "info");
					this.events.emit("task_paused", { taskId: task.id, waveIdx });
					return; // Exit without completing — resume will re-enter
				}

				const wave = waves[waveIdx];

				// Check worker availability before executing wave
				const workersAvailable = await this.checkWorkerAvailability();
				if (!workersAvailable) {
					task.status = "failed";
					task.error = "No workers available — all workers offline or overloaded";
					ctx.ui.notify(`Task ${task.id}: failed — no workers available`, "error");
					break;
				}
				ctx.ui.notify(
					`Task ${task.id}: wave ${waveIdx + 1}/${waves.length} — [${wave.map((s) => s.id).join(", ")}]`,
					"info",
				);
					this.events.emit("wave_start", { taskId: task.id, waveIdx, totalWaves: waves.length, subtaskIds: wave.map(s => s.id) });

				const results = await this.executeWave(wave, task, ctx);

				for (const result of results) {
					const st = task.subtasks.find((s) => s.id === result.id);
					if (st) {
						st.status = result.status;
						st.result = result.result;
						st.error = result.error;
						st.review = result.review;
						st.completedAt = result.completedAt;
					}
				}

				this.updateWidget(ctx, widgetKey, task);
				await this.persist(task);
				this.syncTaskToGrpc(task);
					this.events.emit("wave_end", { taskId: task.id, waveIdx, totalWaves: waves.length, results });

				// Auto-checkpoint after wave completes (dual storage)
				await this.checkpoint(task);

				// Subtask results/reviews already written per-subtask in executeWave
				// (moved to subtask-level for real-time Dashboard visibility)

				const failed = results.filter((r) => r.status === "failed");
				const cancelled = results.filter((r) => r.status === "cancelled");
				if (cancelled.length > 0) {
					// Cascade cancel to downstream subtasks
					this.cascadeCancel(task, cancelled.map((r) => r.id));
					// Only cancel the whole task if this was a task-level cancel
					// @ts-expect-error TS2367 — controlState is mutable, TS narrowing is wrong
					if (task.controlState === "cancelled") {
						task.status = "cancelled";
						task.error = `Cancelled: ${cancelled.map((f) => f.id).join(", ")}`;
						break;
					}
					// Subtask-level cancel: continue with remaining non-cancelled paths
				}
				if (failed.length > 0) {
					// Try re-decomposing failed subtasks before giving up
					const redecomposed = await this.tryRedecompose(task, ctx);
					if (redecomposed) {
						// Re-execute with new subtasks
						const pendingDefs: SubtaskDef[] = task.subtasks
							.filter((s) => s.status === "pending" || s.status === "running")
							.map((s) => ({
								id: s.id,
								description: s.description,
								dependsOn: s.dependsOn,
								files: s.files,
							}));
						const newWaves = splitWavesByFileOverlap(buildDAG(pendingDefs));
						ctx.ui.notify(
							`Task ${task.id}: re-decomposed ${failed.length} failed subtask(s) into ${pendingDefs.length} new one(s)`,
							"info",
						);
						await this.executeWaves(task, newWaves, ctx);
						return;
					}
					task.status = "failed";
					task.error = `${failed.length} subtask(s) failed: ${failed.map((f) => f.id).join(", ")}`;
					break;
				}
			}

			if (task.status === "in_progress") {
				task.status = "completed";
				task.completedAt = Date.now();
			}
		} catch (err) {
			if ((err as Error).name === "AbortError") {
				task.status = "cancelled";
			} else {
				task.status = "failed";
				task.error = `Execution error: ${err instanceof Error ? err.message : String(err)}`;
			}
		}

		ctx.ui.setWidget(widgetKey, undefined);
		await this.persist(task);
		this.syncTaskToGrpc(task);

		const summary = this.buildSummary(task);
		const notifyType = task.status === "completed" ? "info" : "error";
		ctx.ui.notify(`Task ${task.id}: ${task.status}`, notifyType);
			this.events.emit("task_complete", { taskId: task.id, status: task.status, summary });

		this.bridge.writeMemory(
			"task", `task_result_${task.id}`,
			summary, "structured", "uc-orchestrator", task.id,
		).catch(() => {});

		this.pi.sendMessage(
			{
				customType: "uc-task-result",
				content: [{ type: "text", text: summary }],
				display: true,
				details: {
					taskId: task.id,
					status: task.status,
					subtaskCount: task.subtasks.length,
				},
			},
			{ triggerTurn: false },
		);
	}

	private async executeWave(
		wave: SubtaskDef[],
		task: TaskState,
		ctx: ExtensionCommandContext,
	): Promise<SubtaskResult[]> {
		// Filter out already-cancelled subtasks
		const activeWave = wave.filter((def) => {
			const existing = task.subtasks.find((s) => s.id === def.id);
			return !existing || existing.status !== "cancelled";
		});

		if (activeWave.length === 0) return [];

		const abortCtrl = this.abortControllers.get(task.id);
		const results: SubtaskResult[] = [];
		const queue = [...activeWave];
		const intentTracker = new FileIntentTracker();

		const runNext = async (): Promise<void> => {
			while (queue.length > 0) {
				// Check cancel before picking next
				if (task.controlState === "cancelled") {
					abortCtrl?.abort();
					this.runningCount--;
					return;
				}

				// Find next subtask without file conflict
				let def: SubtaskDef | undefined;
				let defIdx = -1;
				for (let i = 0; i < queue.length; i++) {
					const candidate = queue[i];
					if (intentTracker.isConflicting(candidate.files).size === 0) {
						def = candidate;
						defIdx = i;
						break;
					}
				}
				if (!def) {
					// All remaining subtasks conflict — wait for a running one to finish
					await new Promise((resolve) => setTimeout(resolve, 100));
					continue;
				}
				queue.splice(defIdx, 1);

				// Declare file intent before execution
				intentTracker.declare(def.id, def.files);
				let result: SubtaskResult;
				try {
					// Circuit breaker: fail fast if service is degraded
					if (!this.circuitBreaker.canExecute()) {
						result = {
							id: def.id,
							description: def.description,
							status: "failed",
							dependsOn: def.dependsOn,
							files: def.files,
							error: "Circuit breaker open — too many consecutive failures",
							startedAt: Date.now(),
							completedAt: Date.now(),
						};
					} else {
						this.events.emit("subtask_start", { taskId: task.id, subtaskId: def.id, description: def.description });
						result = await this.executeSubtaskWithRetry(def, task, ctx);
						if (result.status === "failed") {
							this.circuitBreaker.recordFailure();
						} else {
							this.circuitBreaker.recordSuccess();
						}
					}
				} finally {
					// Release file intent even on unexpected error to prevent livelock
					intentTracker.release(def.id);
				}
				results.push(result);
				this.runningCount--;
				// Subtask-level event: sync to gRPC + write memory for Dashboard visibility
				const st = task.subtasks.find((s) => s.id === result.id);
				if (st) {
					st.status = result.status;
					st.result = result.result;
					st.error = result.error;
					st.review = result.review;
					st.completedAt = result.completedAt;
				}
				this.syncTaskToGrpc(task);
				if (result.result) {
					this.bridge.writeMemory(
						"task", `subtask_result_${result.id}`,
						result.result, "text", "uc-orchestrator", task.id,
					).catch(() => {});
				}
				if (result.review) {
					this.bridge.writeMemory(
						"task", `subtask_review_${result.id}`,
						JSON.stringify({
							approved: result.review.approved,
							issues: result.review.issues,
							suggestions: result.review.suggestions,
						}),
						"structured", "uc-orchestrator", task.id,
					).catch(() => {});
				}
			}
		};

		const starters = Math.min(this.config.maxConcurrency, activeWave.length);
		const workers: Promise<void>[] = [];
		for (let i = 0; i < starters; i++) {
			this.runningCount++;
			workers.push(runNext());
		}
		await Promise.all(workers);

		intentTracker.clear();
		const order = new Map(wave.map((d, i) => [d.id, i]));
		results.sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
		return results;
	}

	// ── Re-decompose Failed Subtasks ────────────────────────────────────

	/**
	 * Attempt to re-decompose permanently failed subtasks into smaller ones.
	 *
	 * Only tries once per task (one-shot guard). New subtasks depend on all
	 * completed subtasks and have retryCount = maxRetries (no further retries).
	 * Returns true if re-decomposition succeeded and new subtasks were added.
	 */
	private async tryRedecompose(
		task: TaskState,
		ctx: ExtensionCommandContext,
	): Promise<boolean> {
		// One-shot guard
		if (task.redecomposed) return false;

		const failed = task.subtasks.filter((s) => s.status === "failed");
		if (failed.length === 0) return false;

		// Build context from completed subtasks
		const completedSummaries = task.subtasks
			.filter((s) => s.status === "completed" && s.result)
			.map((s) => `- ${s.description}: ${s.result!.slice(0, 200)}`)
			.slice(0, 5);

		const failedDescriptions = failed.map(
			(s) => `- ${s.description} (error: ${s.error?.slice(0, 200) ?? "unknown"})`,
		);

		const redecomposePrompt =
			"The following subtasks of a larger task failed:\n" +
			failedDescriptions.join("\n") +
			"\n\nCompleted subtasks so far:\n" +
			(completedSummaries.length > 0 ? completedSummaries.join("\n") : "(none)") +
			"\n\nOriginal task: " + task.description +
			"\n\nDecompose each failed subtask into 1-2 simpler, more specific subtasks." +
			"\nOutput a JSON object with a 'subtasks' array, each having id, description, depends_on, and files.";

		try {
			const newDefs = await this.decompose(redecomposePrompt, ctx);

			// Remove failed subtasks, add new ones
			const completedIds = task.subtasks
				.filter((s) => s.status === "completed")
				.map((s) => s.id);

			const newSubtasks: SubtaskResult[] = newDefs.map((def) => ({
				id: def.id,
				description: def.description,
				status: "pending" as const,
				dependsOn: [...completedIds, ...def.dependsOn],
				files: def.files,
				retryCount: this.config.maxRetries, // no further retries
			}));

			task.subtasks = [
				...task.subtasks.filter((s) => s.status !== "failed"),
				...newSubtasks,
			];
			task.redecomposed = true;
			task.error = undefined;
			task.status = "in_progress";

			await this.persist(task);
			this.syncTaskToGrpc(task);
			this.pi.logger.info(
				`Re-decomposed ${failed.length} failed subtask(s) into ${newSubtasks.length} new one(s)`,
			);
			return true;
		} catch (err) {
			this.pi.logger.warn(
				`Re-decomposition failed: ${err instanceof Error ? err.message : String(err)}`,
			);
			return false;
		}
	}

	// ── Control: Cancel / Pause / Resume ──────────────────────────────

	async cancelTask(taskId: string, subtaskId?: string, ctx?: ExtensionCommandContext): Promise<boolean> {
		const task = this.tasks.get(taskId);
		if (!task) return false;

		if (subtaskId) {
			// Subtask-level cancel
			const st = task.subtasks.find((s) => s.id === subtaskId);
			if (!st) return false;

			st.status = "cancelled";
			st.completedAt = Date.now();
			this.cascadeCancel(task, [subtaskId]);
			await this.persist(task);
			this.syncTaskToGrpc(task);
			ctx?.ui.notify(`Subtask ${subtaskId} cancelled (cascade applied)`, "info");
			return true;
		}

		// Task-level cancel
		task.controlState = "cancelled";
		task.status = "cancelled";
		task.completedAt = Date.now();

		const abortCtrl = this.abortControllers.get(taskId);
		abortCtrl?.abort();

		// Mark running subtasks as cancelled
		for (const st of task.subtasks) {
			if (st.status === "running" || st.status === "pending" || st.status === "reviewing") {
				st.status = "cancelled";
				st.completedAt = Date.now();
			}
		}

		await this.persist(task);
		this.syncTaskToGrpc(task);
		ctx?.ui.notify(`Task ${taskId} cancelled`, "info");
			this.events.emit("task_cancelled", { taskId });
		return true;
	}

	async pauseTask(taskId: string, ctx?: ExtensionCommandContext): Promise<boolean> {
		const task = this.tasks.get(taskId);
		if (!task) return false;
		if (task.status !== "in_progress" && task.status !== "planning") return false;

		task.controlState = "paused";
		await this.persist(task);
		this.syncTaskToGrpc(task);
		this.bridge.pauseTask(taskId).catch(() => {});
		ctx?.ui.notify(`Task ${taskId} pausing (will stop after current wave)`, "info");
		return true;
	}

	async resumeTask(taskId: string, ctx?: ExtensionCommandContext): Promise<boolean> {
		const task = this.tasks.get(taskId);
		if (!task) return false;
		if (task.controlState !== "paused" && task.status !== "failed") return false;

		task.controlState = "running";
		task.status = "in_progress";
		task.error = undefined;
		task.resumeFromWave = undefined;
		this.abortControllers.set(taskId, new AbortController());
		this.bridge.resumeTask(taskId).catch(() => {});
		this.syncTaskToGrpc(task);

		// Reset failed subtasks back to pending (skip completed ones)
		for (const st of task.subtasks) {
			if (st.status === "failed") {
				st.status = "pending";
				st.error = undefined;
				st.result = undefined;
				st.retryCount = 0;
			}
		}

		// Rebuild waves from current subtask state (pending + running, skip completed/cancelled)
		const pendingDefs: SubtaskDef[] = task.subtasks
			.filter((s) => s.status === "pending" || s.status === "running")
			.map((s) => ({
				id: s.id,
				description: s.description,
				dependsOn: s.dependsOn,
				files: s.files,
			}));

		if (pendingDefs.length === 0) {
			task.status = "completed";
			task.completedAt = Date.now();
			await this.persist(task);
			this.syncTaskToGrpc(task);
			ctx?.ui.notify(`Task ${taskId}: all subtasks already completed`, "info");
			return true;
		}

		const waves = splitWavesByFileOverlap(buildDAG(pendingDefs));
		await this.persist(task);
		ctx?.ui.notify(`Task ${taskId}: resuming with ${pendingDefs.length} pending subtask(s)`, "info");
			this.events.emit("task_resumed", { taskId });

		// Execute remaining waves — stub ctx if not provided (ponytail: rpc server doesn't have omp context)
		const execCtx = ctx ?? stubContext();
		await this.executeWaves(task, waves, execCtx);
		return true;
	}

	/** Cascade cancel to all downstream subtasks that depend on cancelled ones. */
	private cascadeCancel(task: TaskState, cancelledIds: string[]): void {
		const cancelled = new Set(cancelledIds);
		let changed = true;
		while (changed) {
			changed = false;
			for (const st of task.subtasks) {
				if (st.status === "cancelled") continue;
				if (st.dependsOn.some((dep) => cancelled.has(dep))) {
					st.status = "cancelled";
					st.completedAt = Date.now();
					cancelled.add(st.id);
					changed = true;
				}
			}
		}
	}

	// ── Status ───────────────────────────────────────────────────────

	async showStatus(taskId: string | undefined, ctx: ExtensionCommandContext): Promise<void> {
		if (!taskId) {
			if (this.tasks.size === 0) {
				ctx.ui.notify("No tasks submitted yet.", "info");
				return;
			}
			const lines = ["Tasks:"];
			for (const task of this.tasks.values()) {
				const done = task.subtasks.filter((s) => s.status === "completed").length;
				const total = task.subtasks.length;
				const ctrl = task.controlState !== "running" ? ` [${task.controlState}]` : "";
				lines.push(
					`  ${task.id}: ${task.status}${ctrl} (${done}/${total} subtasks) — ${task.description.slice(0, 60)}`,
				);
			}
			ctx.ui.notify(lines.join("\n"), "info");
			return;
		}

		const task = this.tasks.get(taskId);
		if (!task) {
			ctx.ui.notify(`Task ${taskId} not found.`, "error");
			return;
		}

		const lines = [
			`Task: ${task.id}`,
			`Description: ${task.description}`,
			`Status: ${task.status}`,
			`Control: ${task.controlState}`,
			`Subtasks:`,
		];
		for (const st of task.subtasks) {
			const deps = st.dependsOn.length > 0 ? ` (depends: ${st.dependsOn.join(", ")})` : "";
			lines.push(`  ${st.id}: ${st.status}${deps}`);
			if (st.result) lines.push(`    result: ${st.result.slice(0, 100)}`);
			if (st.error) lines.push(`    error: ${st.error}`);
			if (st.retryCount && st.retryCount > 0) lines.push(`    retries: ${st.retryCount}`);
			if (st.review) {
				lines.push(`    review: ${st.review.approved ? "approved" : "rejected"}`);
				if (st.review.issues.length > 0) lines.push(`    issues: ${st.review.issues.join(", ")}`);
			}
		}
		ctx.ui.notify(lines.join("\n"), "info");
	}

	// ── Decomposition ──────────────────────────────────────────────

	private async decompose(
		description: string,
		ctx: ExtensionCommandContext,
	): Promise<SubtaskDef[]> {
		const result = await runSubprocess({
			cwd: ctx.cwd,
			agent: {
				name: "decomposer",
				description: "Decompose a task into ordered subtasks with dependencies",
				systemPrompt: DECOMPOSER_PROMPT,
				source: "project" as const,
				output: {
					type: "object",
					properties: {
						subtasks: {
							type: "array",
							items: {
								type: "object",
								properties: {
									id: { type: "string" },
									description: { type: "string" },
									depends_on: {
										type: "array",
										items: { type: "string" },
									},
									files: {
										type: "array",
										items: { type: "string" },
									},
								},
							},
						},
					},
				},
			},
			task: description,
			id: `decompose-${Date.now().toString(36)}`,
			index: 0,
			signal: AbortSignal.timeout(120_000),
			modelRegistry: ctx.modelRegistry,
			settings: this.pi.pi.settings,
			enableLsp: false,
		});

		if (result.exitCode !== 0) {
			throw new Error(
				`Decomposer agent failed: ${result.stderr.slice(0, 500) || "unknown error"}`,
			);
		}

		const output = this.parseSubtaskOutput(result.output, description);
		if (output.length === 0) {
			throw new Error("Decomposer returned no subtasks");
		}

		return output;
	}

	private parseSubtaskOutput(raw: string, _description: string): SubtaskDef[] {
		try {
			const parsed = JSON.parse(raw);
			if (parsed.subtasks && Array.isArray(parsed.subtasks)) {
				return parsed.subtasks.map((st: Record<string, unknown>, i: number) => ({
					id: (st.id as string) || `st-${i + 1}`,
					description: st.description as string,
					dependsOn: (st.depends_on as string[]) || [],
					files: (st.files as string[]) || [],
				}));
			}
		} catch {
			// Not JSON — fall through
		}

		// ponytail: text fallback
		const lines = raw.split("\n").filter((l) => l.trim());
		const subtasks: SubtaskDef[] = [];
		for (let i = 0; i < lines.length; i++) {
			const match = lines[i].match(/^\d+\.\s+(.+)/);
			if (match) {
				subtasks.push({
					id: `st-${i + 1}`,
					description: match[1].trim(),
					dependsOn: i > 0 ? [`st-${i}`] : [],
					files: [],
				});
			}
		}
		return subtasks;
	}

	// ── Context Injection ────────────────────────────────────────────

	/** Build context string from completed subtasks for a given subtask. */
	private buildContextForSubtask(def: SubtaskDef, task: TaskState): string {
		const MAX_SUMMARY_LEN = 500;

		const completedSubtasks = task.subtasks.filter(
			(s) => s.status === "completed" && def.dependsOn.includes(s.id),
		);
		if (completedSubtasks.length === 0) return "";

		const parts: string[] = [];
		let totalLen = 0;
		for (const s of completedSubtasks) {
			const result = s.result ? s.result.slice(0, 200) : "(no result)";
			const line = `  - ${s.id}: ${s.description}\n    Result: ${result}`;
			if (totalLen + line.length > MAX_SUMMARY_LEN) {
				// Truncate to fit within budget
				const remaining = MAX_SUMMARY_LEN - totalLen;
				if (remaining > 0) parts.push(line.slice(0, remaining) + "...");
				break;
			}
			parts.push(line);
			totalLen += line.length + 1; // +1 for newline separator
		}

		return `[Completed prerequisite subtasks]\n${parts.join("\n")}\n`;
	}

	// ── Subtask Execution ──────────────────────────────────────────

	private async executeSubtaskWithRetry(
		def: SubtaskDef,
		task: TaskState,
		ctx: ExtensionCommandContext,
	): Promise<SubtaskResult> {
		let lastResult: SubtaskResult | null = null;

		for (let attempt = 0; attempt <= this.config.maxRetries; attempt++) {
			const result = await this.executeSubtask(def, task, ctx);
			result.retryCount = attempt;

			if (result.status === "completed" || result.status === "cancelled" || (result.error?.startsWith("Review rejected"))) {
				return result;
			}

			lastResult = result;

			if (attempt < this.config.maxRetries) {
				const delay = this.config.retryBaseDelayMs * Math.pow(2, attempt);
				this.pi.logger.info(
					`Retrying subtask ${def.id} (attempt ${attempt + 1}/${this.config.maxRetries}) after ${delay}ms`,
				);
				await new Promise((resolve) => setTimeout(resolve, delay));
			}
		}

		// Retries exhausted — mark failed and notify Dashboard
		lastResult!.retryCount = this.config.maxRetries;
		this.pi.logger.warn(
			`Subtask ${def.id} failed permanently after ${this.config.maxRetries} retries`,
		);
		this.syncTaskToGrpc(task);
		this.bridge.writeMemory(
			"task", `subtask_failed_${def.id}`,
			JSON.stringify({
				subtask_id: def.id,
				retry_count: this.config.maxRetries,
				error: lastResult!.error,
				stderr_tail: lastResult!.stderrTail,
			}),
			"structured", "uc-orchestrator", task.id,
		).catch(() => {});

		return lastResult!;
	}

	private async executeSubtask(
		def: SubtaskDef,
		task: TaskState,
		ctx: ExtensionCommandContext,
	): Promise<SubtaskResult> {
		const result: SubtaskResult = {
			id: def.id,
			description: def.description,
			status: "running",
			dependsOn: def.dependsOn,
			files: def.files,
			startedAt: Date.now(),
		};

		// Inject context from completed prerequisites
		const contextPrefix = this.buildContextForSubtask(def, task);
		const taskPrompt = contextPrefix
			? `${contextPrefix}\n## Your subtask\n${def.description}`
			: def.description;

		try {
			const abortCtrl = this.abortControllers.get(task.id);
			const subResult = await runSubprocess({
				cwd: ctx.cwd,
				agent: {
					name: "worker",
					description: `Execute subtask: ${def.description}`,
					systemPrompt: WORKER_PROMPT,
					source: "project" as const,
				},
				task: taskPrompt,
				id: def.id,
				index: 0,
				signal: abortCtrl?.signal ?? AbortSignal.timeout(300_000),
				modelRegistry: ctx.modelRegistry,
				settings: this.pi.pi.settings,
				enableLsp: true,
			});

			if (subResult.exitCode === 0) {
				result.result = subResult.output.slice(0, 2000) || "(completed)";
				// ponytail: extract modified files from patchPath if available, else from output
				result.modifiedFiles = subResult.patchPath
					? ["(patch available)"]
					: extractModifiedFiles(subResult.output);
				result.recentToolCalls = extractRecentToolCalls(subResult.output, 5);

				if (this.config.enableReview) {
					result.status = "reviewing";
						this.events.emit("subtask_reviewing", { taskId: task.id, subtaskId: result.id });
					try {
						const review = await this.reviewSubtask(def, result.result, ctx);
						result.review = review;
						if (review.approved) {
							result.status = "completed";
						} else {
							result.status = "failed";
							result.error = `Review rejected: ${review.issues.join("; ")}`;
						}
					} catch {
						result.status = "completed";
					}
				} else {
					result.status = "completed";
				}
			} else {
				result.status = "failed";
				result.error = (subResult.stderr ?? "").slice(0, 500) || "unknown error";
				result.stderrTail = (subResult.stderr ?? "").slice(-500) || undefined;
			}
		} catch (err) {
			if ((err as Error).name === "AbortError") {
				result.status = "cancelled";
			} else {
				result.status = "failed";
				result.error = err instanceof Error ? err.message : String(err);
			}
		}

		result.completedAt = Date.now();
		// Emit subtask-level events for real-time UI updates
		if (result.status === "completed") {
			this.events.emit("subtask_end", { taskId: task.id, subtaskId: result.id, result: result.result });
		} else if (result.status === "failed") {
			this.events.emit("subtask_failed", { taskId: task.id, subtaskId: result.id, error: result.error, retryCount: result.retryCount });
		} else if (result.status === "cancelled") {
			this.events.emit("subtask_failed", { taskId: task.id, subtaskId: result.id, error: "cancelled" });
		}
		return result;
	}

	// ── Supervisor Review ──────────────────────────────────────────

	private async reviewSubtask(
		def: SubtaskDef,
		workerOutput: string,
		ctx: ExtensionCommandContext,
	): Promise<ReviewResult> {
		const result = await runSubprocess({
			cwd: ctx.cwd,
			agent: {
				name: "supervisor",
				description: `Review subtask result: ${def.description}`,
				systemPrompt: SUPERVISOR_PROMPT,
				source: "project" as const,
				output: {
					type: "object",
					properties: {
						approved: { type: "boolean" },
						issues: { type: "array", items: { type: "string" } },
						suggestions: { type: "array", items: { type: "string" } },
					},
				},
			},
			task: [
				`## Subtask: ${def.description}`,
				`## Files: ${def.files.join(", ") || "(auto-detected)"}`,
				`## Worker Output:`,
				workerOutput,
			].join("\n"),
			id: `review-${def.id}`,
			index: 0,
			signal: AbortSignal.timeout(this.config.reviewTimeoutMs),
			modelRegistry: ctx.modelRegistry,
			settings: this.pi.pi.settings,
			enableLsp: true,
		});

		if (result.exitCode !== 0) {
			throw new Error(`Supervisor agent failed: ${result.stderr.slice(0, 500)}`);
		}

		try {
			const parsed = JSON.parse(result.output);
			return {
				approved: parsed.approved ?? true,
				issues: parsed.issues ?? [],
				suggestions: parsed.suggestions ?? [],
			};
		} catch {
			return { approved: true, issues: [], suggestions: [] };
		}
	}

	// ── Persistence ──────────────────────────────────────────────────

	private async persist(task: TaskState): Promise<void> {
		await this.store.save(this.toPersisted(task));
	}

	/** Auto-checkpoint: local file (primary) + gRPC sync (secondary, fire-and-forget). */
	private async checkpoint(task: TaskState): Promise<void> {
		const snap = this.toPersisted(task);
		// Primary: local file
		await this.store.saveCheckpoint(snap);
		// Secondary: gRPC sync (fire-and-forget)
		this.bridge.writeMemory(
			"task", `checkpoint_snap-${task.id}-${Date.now().toString(36)}`,
			JSON.stringify({ ...snap, _v: 1 }),
			"structured", "uc-orchestrator", task.id,
		).catch(() => {});
	}

	private toPersisted(task: TaskState): PersistedTask {
		return {
			id: task.id,
			description: task.description,
			status: task.status,
			error: task.error,
			controlState: task.controlState,
			resumeFromWave: task.resumeFromWave,
			redecomposed: task.redecomposed,
			subtasks: task.subtasks.map((s) => ({
				id: s.id,
				description: s.description,
				status: s.status,
				dependsOn: s.dependsOn,
				files: s.files,
				result: s.result,
				error: s.error,
				review: s.review,
				startedAt: s.startedAt,
				completedAt: s.completedAt,
				modifiedFiles: s.modifiedFiles,
				recentToolCalls: s.recentToolCalls,
				stderrTail: s.stderrTail,
				retryCount: s.retryCount,
			})),
			createdAt: task.createdAt,
			completedAt: task.completedAt,
		};
	}

	private fromPersisted(p: PersistedTask): TaskState {
		return {
			id: p.id,
			description: p.description,
			status: p.status as TaskState["status"],
			controlState: p.controlState,
			error: p.error,
			resumeFromWave: p.resumeFromWave,
			redecomposed: p.redecomposed,
			subtasks: p.subtasks.map((s) => ({
				id: s.id,
				description: s.description,
				status: s.status as SubtaskResult["status"],
				dependsOn: s.dependsOn,
				files: s.files ?? [],
				result: s.result,
				error: s.error,
				review: s.review,
				startedAt: s.startedAt,
				completedAt: s.completedAt,
				modifiedFiles: s.modifiedFiles,
				recentToolCalls: s.recentToolCalls,
				stderrTail: s.stderrTail,
				retryCount: s.retryCount,
			})),
			createdAt: p.createdAt,
			completedAt: p.completedAt,
		};
	}

	// ── gRPC Sync ────────────────────────────────────────────────

	/** Sync task state to UC gRPC TaskStore. Fire-and-forget. */
	private syncTaskToGrpc(task: TaskState): void {
		// ponytail: fire-and-forget — don't await, don't block orchestrator
		this.bridge.upsertTask(this.toPersisted(task)).catch(() => {
			// gRPC sync is best-effort; failure is non-fatal
		});
	}

	// ── UI Helpers ─────────────────────────────────────────────────

	private updateWidget(
		ctx: ExtensionCommandContext,
		key: string,
		task: TaskState,
	): void {
		const ctrl = task.controlState !== "running" ? ` [${task.controlState}]` : "";
		const lines = [`UC Task: ${task.id}`, `Status: ${task.status}${ctrl}`, "Subtasks:"];
		for (const st of task.subtasks) {
			const icon =
				st.status === "completed"
					? "✓"
					: st.status === "running"
						? "●"
						: st.status === "reviewing"
							? "◉"
							: st.status === "failed"
								? "✗"
								: st.status === "cancelled"
									? "⊘"
									: "○";
			lines.push(`  ${icon} ${st.id}: ${st.description.slice(0, 50)}`);
		}
		ctx.ui.setWidget(key, lines);
	}

	// ── Public getters (for uc-rpc-server) ────────────────────────

	getTaskState(id: string): TaskState | undefined {
		return this.tasks.get(id);
	}

	getAllTaskStates(): TaskState[] {
		return [...this.tasks.values()];
	}

	/** Return IDs of non-terminal tasks (for ControlSignalHandler polling). */
	getActiveTaskIds(): string[] {
		return [...this.tasks.values()]
			.filter((t) => t.status !== "completed" && t.status !== "cancelled" && t.status !== "failed")
			.map((t) => t.id);
	}

	private buildSummary(task: TaskState): string {
		const lines: string[] = [];
		lines.push(`## UC Task: ${task.id}`);
		lines.push("");
		lines.push(`- **Description**: ${task.description}`);
		lines.push(`- **Status**: ${task.status}`);
		lines.push(`- **Control**: ${task.controlState}`);
		lines.push(`- **Subtasks**: ${task.subtasks.length}`);
		const completed = task.subtasks.filter((s) => s.status === "completed").length;
		lines.push(`- **Completed**: ${completed}/${task.subtasks.length}`);
		lines.push("");

		lines.push("### Subtask Results");
		lines.push("");
		for (const st of task.subtasks) {
			lines.push(`- **${st.id}** (${st.status}): ${st.description}`);
			if (st.result) lines.push(`  > ${st.result.slice(0, 200)}`);
			if (st.error) lines.push(`  ⚠ ${st.error.slice(0, 200)}`);
			if (st.review && !st.review.approved) {
				lines.push(`  ⚡ Review issues: ${st.review.issues.join(", ")}`);
			}
		}

		return lines.join("\n");
	}
}

// ── Agent Prompts ──────────────────────────────────────────────────

const DECOMPOSER_PROMPT = `You are a task decomposition specialist for a coding system.

Given a high-level task description:
1. Use search/read tools to understand the codebase structure
2. Break the task into minimal, independently verifiable subtasks
3. Define dependency order (which subtasks must complete before others)
4. Identify critical files for each subtask

Rules:
- Each subtask should be completable by a single coding agent in one session
- Subtask IDs should be short: st-1, st-2, etc.
- depends_on lists IDs of subtasks that must complete first
- Keep subtasks between 2-8 items; prefer fewer, larger subtasks over many tiny ones
- If the task is simple enough for one agent, return a single subtask

Output a JSON object with a "subtasks" array. Each item has:
- id: string (e.g. "st-1")
- description: string (what to do)
- depends_on: string[] (IDs of prerequisite subtasks)
- files: string[] (critical file paths)`;

const WORKER_PROMPT = `You are a coding worker agent. Execute the assigned subtask:

1. Read and understand the relevant code
2. Make the necessary changes
3. Verify your changes work (run tests if available)
4. Report what you did

If the prompt includes [Completed prerequisite subtasks], use that context
to understand what was already done by previous workers. You can also use
the uc_memory tool to read detailed results from prior subtasks:
  - uc_memory(action="read", scope="task", key="subtask_result_<id>") for full output
  - uc_memory(action="read", scope="task", key="subtask_review_<id>") for review feedback
  - uc_memory(action="search", scope="task", key="<query>") for semantic search across results

Be thorough but efficient. Focus on the specific subtask — do not expand scope.`;

const SUPERVISOR_PROMPT = `You are a code review specialist. Given a subtask and its result:

1. Verify the changes accomplish the stated goal
2. Check for bugs, style issues, missing error handling
3. Confirm tests (if any) pass logically
4. Output structured approval result

Be strict but fair. Minor style nits are not blockers.
Focus on correctness, security, and completeness.

Output a JSON object with:
- approved: boolean (true if the subtask is satisfactorily completed)
- issues: string[] (list of problems found, empty if approved)
- suggestions: string[] (optional improvements, not blockers)`;

// ponytail: stub ExtensionCommandContext for RPC server (no omp runtime)
function stubContext(): ExtensionCommandContext {
	return {
		cwd: process.cwd(),
		ui: {
			notify: () => {},
			setWidget: () => {},
		},
	} as unknown as ExtensionCommandContext;
}
