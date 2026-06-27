/**
 * Task Bridge — registers uc_task tool with omp ExtensionAPI.
 *
 * Wraps task lifecycle operations (submit/cancel/pause/resume/status)
 * as an LLM-callable tool so agents can orchestrate tasks autonomously.
 */

import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { GrpcBridge, type SubmitResult } from "./grpc-bridge";
import type { UCOrchestrator } from "./orchestrator";

// ponytail: `as never` on parameters to dodge TS2589 deep instantiation
// in registerTool<TParams extends TSchema>. Runtime schema is correct.

export function registerTaskTools(pi: ExtensionAPI, bridge: GrpcBridge, orchestrator?: UCOrchestrator): void {
	const taskSchema = pi.zod.object({
		action: pi.zod.enum(["submit", "cancel", "pause", "resume", "status"]).describe("Task action"),
		task_id: pi.zod.string().optional().describe("Task ID (required for cancel/pause/resume/status)"),
		description: pi.zod.string().optional().describe("Task description (required for submit)"),
		subtask_id: pi.zod.string().optional().describe("Subtask ID (optional, for cancel of specific subtask)"),
	});

	pi.registerTool({
		name: "uc_task",
		label: "UC Task",
		description:
			"Manage UltimateCoders distributed coding tasks. " +
			"Submit new tasks for orchestration (auto-decompose into DAG waves), " +
			"cancel/pause/resume running tasks, or check task status with subtask details.",
		parameters: taskSchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as { action: string; task_id?: string; description?: string; subtask_id?: string };
			try {
				switch (p.action) {
					case "submit": {
						if (!p.description) {
							return {
								content: [{ type: "text" as const, text: "Error: description required for submit" }],
								isError: true,
							};
						}
						const result = await bridge.submitTask(p.description);
						if (!result.ok) {
							// Fall back to local orchestrator when gRPC is unavailable
							if (result.error.kind === "server_unavailable" && orchestrator) {
								pi.logger.info("uc_task submit: gRPC unavailable, falling back to local orchestrator");
								const taskId = await orchestrator.submitTask(p.description);
								return {
									content: [{
										type: "text" as const,
										text: `Task submitted (local): ${taskId}\nStatus: planning\nSubtasks:\n  (pending decomposition)`,
									}],
								};
							}
							const errText = result.error.kind === "server_unavailable"
								? `Submit failed — ${result.error.message}`
								: result.error.kind === "worker_failed"
									? `Worker failed to start: ${result.error.message}`
									: `Submit rejected: ${result.error.message}`;
							return {
								content: [{ type: "text" as const, text: errText }],
								isError: true,
							};
						}
						const subtaskLines = result.task.subtasks
							.slice(0, 10)
							.map((st) => `  ${st.status} ${st.id.slice(0, 8)}: ${st.description.slice(0, 60)}`)
							.join("\n");
						return {
							content: [{
								type: "text" as const,
								text: `Task submitted: ${result.task.taskId}\nStatus: ${result.task.status}\nSubtasks:\n${subtaskLines || "  (pending decomposition)"}`,
							}],
						};
					}
					case "cancel": {
						if (!p.task_id) {
							return {
								content: [{ type: "text" as const, text: "Error: task_id required for cancel" }],
								isError: true,
							};
						}
						const ok = await bridge.cancelTask(p.task_id, p.subtask_id);
						return {
							content: [{
								type: "text" as const,
								text: ok
									? `Cancelled ${p.subtask_id ? `subtask ${p.subtask_id} in` : ""} task ${p.task_id}`
									: `Cancel failed: task ${p.task_id} not found or not cancellable`,
							}],
						};
					}
					case "pause": {
						if (!p.task_id) {
							return {
								content: [{ type: "text" as const, text: "Error: task_id required for pause" }],
								isError: true,
							};
						}
						const ok = await bridge.pauseTask(p.task_id);
						return {
							content: [{
								type: "text" as const,
								text: ok ? `Paused task ${p.task_id}` : `Pause failed: task ${p.task_id} not found or not in progress`,
							}],
						};
					}
					case "resume": {
						if (!p.task_id) {
							return {
								content: [{ type: "text" as const, text: "Error: task_id required for resume" }],
								isError: true,
							};
						}
						const ok = await bridge.resumeTask(p.task_id);
						return {
							content: [{
								type: "text" as const,
								text: ok ? `Resumed task ${p.task_id}` : `Resume failed: task ${p.task_id} not found or not pausable`,
							}],
						};
					}
					case "status": {
						if (p.task_id) {
							const task = await bridge.getTask(p.task_id);
							if (!task) {
								return {
									content: [{ type: "text" as const, text: `Task ${p.task_id} not found` }],
									useless: true,
								};
							}
							const subtaskLines = task.subtasks
								.map((st) => `  [${st.status}] ${st.id.slice(0, 8)}: ${st.description.slice(0, 60)}`)
								.join("\n");
							return {
								content: [{
									type: "text" as const,
									text: `Task ${task.taskId}\nStatus: ${task.status}\nProject: ${task.projectId}\nSubtasks:\n${subtaskLines || "  (none)"}`,
								}],
							};
						}
						// List all tasks
						const tasks = await bridge.listTasks();
						if (tasks.length === 0) {
							return { content: [{ type: "text" as const, text: "(no tasks)" }], useless: true };
						}
						const lines = tasks
							.slice(0, 20)
							.map((t) => `[${t.status}] ${t.taskId.slice(0, 8)}: ${t.description.slice(0, 60)}`);
						return { content: [{ type: "text" as const, text: lines.join("\n") }] };
					}
					default:
						return {
							content: [{ type: "text" as const, text: `Unknown action: ${p.action}` }],
							isError: true,
						};
				}
			} catch (err) {
				return {
					content: [{
						type: "text" as const,
						text: `UC Task error: ${err instanceof Error ? err.message : String(err)}`,
					}],
					isError: true,
				};
			}
		},
	});
}
