/**
 * UCOrchestrator — Core task orchestration logic.
 *
 * Manages the lifecycle of a task:
 * 1. Decompose task into subtasks via omp decomposer agent
 * 2. Build DAG from subtask dependencies
 * 3. Execute waves of subtasks via omp runSubprocess
 * 4. Optionally review subtask results via supervisor agent
 * 5. Collect results and inject summary into conversation
 * 6. Sync state to UC gRPC TaskStore for Dashboard visibility
 */

import type { ExtensionAPI, ExtensionCommandContext } from "@oh-my-pi/pi-coding-agent";
import { runSubprocess } from "@oh-my-pi/pi-coding-agent";
import { buildDAG, type SubtaskDef } from "./scheduler";
import { GrpcBridge } from "./grpc-bridge";

// ── Types ──────────────────────────────────────────────────────────

interface TaskState {
	id: string;
	description: string;
	status: "planning" | "in_progress" | "completed" | "failed";
	subtasks: SubtaskResult[];
	createdAt: number;
	completedAt?: number;
	error?: string;
}

interface SubtaskResult {
	id: string;
	description: string;
	status: "pending" | "running" | "reviewing" | "completed" | "failed";
	dependsOn: string[];
	result?: string;
	error?: string;
	review?: ReviewResult;
	startedAt?: number;
	completedAt?: number;
}

interface ReviewResult {
	approved: boolean;
	issues: string[];
	suggestions: string[];
}

interface OrchestratorConfig {
	/** Enable supervisor review after each subtask. Default: true. */
	enableReview: boolean;
	/** Timeout for supervisor review (ms). Default: 60000. */
	reviewTimeoutMs: number;
}

// ── Orchestrator ───────────────────────────────────────────────────

export class UCOrchestrator {
	private pi: ExtensionAPI;
	private tasks: Map<string, TaskState> = new Map();
	private taskCounter = 0;
	private config: OrchestratorConfig;
	private bridge: GrpcBridge;

	constructor(pi: ExtensionAPI, config?: Partial<OrchestratorConfig>, bridge?: GrpcBridge) {
		this.pi = pi;
		this.config = { enableReview: true, reviewTimeoutMs: 60_000, ...config };
		this.bridge = bridge ?? new GrpcBridge();
	}

	async submitTask(description: string, ctx: ExtensionCommandContext): Promise<void> {
		const taskId = `uc-${++this.taskCounter}-${Date.now().toString(36)}`;

		const task: TaskState = {
			id: taskId,
			description,
			status: "planning",
			subtasks: [],
			createdAt: Date.now(),
		};
		this.tasks.set(taskId, task);

		ctx.ui.notify(`Task ${taskId}: planning...`, "info");

		// ── Step 1: Decompose ──
		let subtaskDefs: SubtaskDef[];
		try {
			subtaskDefs = await this.decompose(description, ctx);
		} catch (err) {
			task.status = "failed";
			task.error = `Decomposition failed: ${err instanceof Error ? err.message : String(err)}`;
			ctx.ui.notify(`Task ${taskId} failed: ${task.error}`, "error");
			this.syncTaskToGrpc(task);
			return;
		}

		// ── Step 2: Build DAG ──
		const waves = buildDAG(subtaskDefs);

		// Initialize subtask results
		task.subtasks = subtaskDefs.map((def) => ({
			id: def.id,
			description: def.description,
			status: "pending",
			dependsOn: def.dependsOn,
		}));
		task.status = "in_progress";
		this.syncTaskToGrpc(task);

		ctx.ui.notify(
			`Task ${taskId}: ${subtaskDefs.length} subtasks, ${waves.length} wave(s)`,
			"info",
		);

		// ── Step 3: Execute waves ──
		const widgetKey = `uc-${taskId}`;
		this.updateWidget(ctx, widgetKey, task);

		try {
			for (let waveIdx = 0; waveIdx < waves.length; waveIdx++) {
				const wave = waves[waveIdx];
				ctx.ui.notify(
					`Task ${taskId}: wave ${waveIdx + 1}/${waves.length} — [${wave.map((s) => s.id).join(", ")}]`,
					"info",
				);

				const results = await Promise.all(
					wave.map((subtaskDef) => this.executeSubtask(subtaskDef, ctx)),
				);

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
				this.syncTaskToGrpc(task);

				const failed = results.filter((r) => r.status === "failed");
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
			task.status = "failed";
			task.error = `Execution error: ${err instanceof Error ? err.message : String(err)}`;
		}

		// Clear widget
		ctx.ui.setWidget(widgetKey, undefined);

		// ── Step 4: Inject summary ──
		this.syncTaskToGrpc(task);

		const summary = this.buildSummary(task);
		const notifyType = task.status === "completed" ? "info" : "error";
		ctx.ui.notify(`Task ${taskId}: ${task.status}`, notifyType);

		// Write task result to UC memory (fire-and-forget)
		this.bridge.writeMemory(
			"task", `task_result_${task.id}`,
			summary, "structured", "uc-orchestrator", task.id,
		);

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
				lines.push(
					`  ${task.id}: ${task.status} (${done}/${total} subtasks) — ${task.description.slice(0, 60)}`,
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

	// ── Subtask Execution ──────────────────────────────────────────

	private async executeSubtask(
		def: SubtaskDef,
		ctx: ExtensionCommandContext,
	): Promise<SubtaskResult> {
		const result: SubtaskResult = {
			id: def.id,
			description: def.description,
			status: "running",
			dependsOn: def.dependsOn,
			startedAt: Date.now(),
		};

		try {
			const subResult = await runSubprocess({
				cwd: ctx.cwd,
				agent: {
					name: "worker",
					description: `Execute subtask: ${def.description}`,
					systemPrompt: WORKER_PROMPT,
					source: "project" as const,
				},
				task: def.description,
				id: def.id,
				index: 0,
				signal: AbortSignal.timeout(300_000),
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
				result.error = subResult.stderr.slice(0, 500) || "unknown error";
			}
		} catch (err) {
			result.status = "failed";
			result.error = err instanceof Error ? err.message : String(err);
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

	// ── gRPC Sync ────────────────────────────────────────────────

	/** Fire-and-forget sync of task state to UC gRPC TaskStore. */
	private syncTaskToGrpc(task: TaskState): void {
		// ponytail: fire-and-forget — don't await, don't block orchestrator
		this.bridge.submitTask(task.description).catch(() => {
			// gRPC sync is best-effort; failure is non-fatal
		});
	}

	// ── UI Helpers ─────────────────────────────────────────────────

	private updateWidget(
		ctx: ExtensionCommandContext,
		key: string,
		task: TaskState,
	): void {
		const lines = [`UC Task: ${task.id}`, `Status: ${task.status}`, "Subtasks:"];
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
