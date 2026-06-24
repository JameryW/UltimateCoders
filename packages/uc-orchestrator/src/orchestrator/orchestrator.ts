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
import { buildDAG, type SubtaskDef } from "./scheduler";
import { GrpcBridge } from "./grpc-bridge";
import { TaskStore, type PersistedTask } from "./task-store";

// ── Types ──────────────────────────────────────────────────────────

type ControlState = "running" | "paused" | "cancelled";

interface TaskState {
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
}

interface SubtaskResult {
	id: string;
	description: string;
	status: "pending" | "running" | "reviewing" | "completed" | "failed" | "cancelled";
	dependsOn: string[];
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
	}

	/** Restore recoverable tasks from disk. Call once at startup. */
	async restore(): Promise<void> {
		await this.store.init();
		const recoverable = await this.store.loadRecoverable();
		for (const p of recoverable) {
			const task = this.fromPersisted(p);
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
	}

	// ── Task Submission ──────────────────────────────────────────────

	async submitTask(description: string, ctx: ExtensionCommandContext): Promise<void> {
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

		ctx.ui.notify(`Task ${taskId}: planning...`, "info");
		await this.persist(task);

		// ── Step 1: Decompose ──
		let subtaskDefs: SubtaskDef[];
		try {
			subtaskDefs = await this.decompose(description, ctx);
		} catch (err) {
			task.status = "failed";
			task.error = `Decomposition failed: ${err instanceof Error ? err.message : String(err)}`;
			ctx.ui.notify(`Task ${taskId} failed: ${task.error}`, "error");
			await this.persist(task);
			this.syncTaskToGrpc(task);
			return;
		}

		// ── Step 2: Build DAG ──
		const waves = buildDAG(subtaskDefs);

		task.subtasks = subtaskDefs.map((def) => ({
			id: def.id,
			description: def.description,
			status: "pending",
			dependsOn: def.dependsOn,
		}));
		task.status = "in_progress";
		await this.persist(task);
		this.syncTaskToGrpc(task);

		ctx.ui.notify(
			`Task ${taskId}: ${subtaskDefs.length} subtasks, ${waves.length} wave(s)`,
			"info",
		);

		// ── Step 3: Execute waves ──
		await this.executeWaves(task, waves, ctx);
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
					return; // Exit without completing — resume will re-enter
				}

				const wave = waves[waveIdx];
				ctx.ui.notify(
					`Task ${task.id}: wave ${waveIdx + 1}/${waves.length} — [${wave.map((s) => s.id).join(", ")}]`,
					"info",
				);

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

				// Write completed subtask results to UC memory (fire-and-forget)
				for (const result of results) {
					if (result.status === "completed" && result.result) {
						this.bridge.writeMemory(
							"task", `subtask_result_${result.id}`,
							result.result, "text", "uc-orchestrator", task.id,
						).catch(() => {});
					}
				}

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

		const runNext = async (): Promise<void> => {
			while (queue.length > 0) {
				// Check cancel before picking next
				if (task.controlState === "cancelled") {
					abortCtrl?.abort();
					this.runningCount--;
					return;
				}

				const def = queue.shift()!;
				const result = await this.executeSubtaskWithRetry(def, task, ctx);
				results.push(result);
				this.runningCount--;
			}
		};

		const starters = Math.min(this.config.maxConcurrency, activeWave.length);
		const workers: Promise<void>[] = [];
		for (let i = 0; i < starters; i++) {
			this.runningCount++;
			workers.push(runNext());
		}
		await Promise.all(workers);

		const order = new Map(wave.map((d, i) => [d.id, i]));
		results.sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
		return results;
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

	async resumeTask(taskId: string, ctx: ExtensionCommandContext): Promise<boolean> {
		const task = this.tasks.get(taskId);
		if (!task) return false;
		if (task.controlState !== "paused") return false;

		task.controlState = "running";
		task.status = "in_progress";
		task.resumeFromWave = undefined; // Clear — will rebuild waves from scratch
		this.abortControllers.set(taskId, new AbortController());
		this.bridge.resumeTask(taskId).catch(() => {});
		this.syncTaskToGrpc(task);

		// Rebuild waves from current subtask state
		const pendingDefs: SubtaskDef[] = task.subtasks
			.filter((s) => s.status === "pending" || s.status === "running")
			.map((s) => ({
				id: s.id,
				description: s.description,
				dependsOn: s.dependsOn,
				files: [],
			}));

		if (pendingDefs.length === 0) {
			task.status = "completed";
			task.completedAt = Date.now();
			await this.persist(task);
			this.syncTaskToGrpc(task);
			ctx.ui.notify(`Task ${taskId}: all subtasks already completed`, "info");
			return true;
		}

		const waves = buildDAG(pendingDefs);
		await this.persist(task);
		ctx.ui.notify(`Task ${taskId}: resuming with ${pendingDefs.length} pending subtask(s)`, "info");

		// Execute remaining waves
		await this.executeWaves(task, waves, ctx);
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

				if (this.config.enableReview) {
					result.status = "reviewing";
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

	private toPersisted(task: TaskState): PersistedTask {
		return {
			id: task.id,
			description: task.description,
			status: task.status,
			error: task.error,
			controlState: task.controlState,
			subtasks: task.subtasks.map((s) => ({
				id: s.id,
				description: s.description,
				status: s.status,
				dependsOn: s.dependsOn,
				result: s.result,
				error: s.error,
				review: s.review,
				startedAt: s.startedAt,
				completedAt: s.completedAt,
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
			subtasks: p.subtasks.map((s) => ({
				id: s.id,
				description: s.description,
				status: s.status as SubtaskResult["status"],
				dependsOn: s.dependsOn,
				result: s.result,
				error: s.error,
				review: s.review,
				startedAt: s.startedAt,
				completedAt: s.completedAt,
			})),
			createdAt: p.createdAt,
			completedAt: p.completedAt,
			resumeFromWave: p.controlState === "paused" ? 0 : undefined,
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
the uc_memory tool to read detailed results from prior subtasks.

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
