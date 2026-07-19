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

/**
 * Unified spawn-gating switch. When `UC_NO_SPAWN` env is set, all task
 * submission entry points (uc_task tool, /uc submit, submit_task RPC)
 * refuse to dispatch new subtasks. Non-spawning tools are unaffected.
 */
export function isSpawnDisabled(): boolean {
	return Boolean(process.env.UC_NO_SPAWN);
}

// ponytail: F40 — tool-output wording for local orchestrator failures when
// gRPC is down (mirrors extension.ts's controlFailureMessage for slash
// commands). Without this, a down server read as "not found"/"not in
// progress" and LLMs concluded the task didn't exist — re-submitting work.
function localFailureMessage(verb: string, tid: string, r: { reason: string; candidates?: string[] }): string {
	const list = (r.candidates ?? []).join(", ") || "(none)";
	switch (r.reason) {
		case "not_found":
			return `${verb} failed: gRPC server unavailable, and no local task matches "${tid}". Recent local tasks: ${list}`;
		case "ambiguous":
			return `${verb} failed: "${tid}" matches multiple local tasks: ${list}`;
		case "bad_state":
			return `${verb} failed: local task "${tid}" is not in a ${verb.toLowerCase()}-able state`;
		default:
			return `${verb} failed (${r.reason})`;
	}
}

// ponytail: F40 — local TaskState rendered in the bridge-status shape, with
// a banner saying the server view is missing (no steps tag: TaskState
// subtasks carry no steps).
function renderLocalTask(t: { id: string; status: string; subtasks: Array<{ id: string; status: string; description: string }> }): string {
	const lines = t.subtasks.map((st) => `  [${st.status}] ${st.id.slice(0, 8)}: ${st.description.slice(0, 60)}`);
	return `Task ${t.id}\nStatus: ${t.status}\nSubtasks:\n${lines.join("\n") || "  (none)"}\n(local view — gRPC server unavailable)`;
}

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
						if (isSpawnDisabled()) {
							return {
								// ponytail: F38 — English like every other tool message.
								content: [{ type: "text" as const, text: "Subtask spawning disabled (UC_NO_SPAWN). Use /uc status to view existing tasks." }],
								isError: true,
							};
						}
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
							.map((st) => {
								const stepCount = st.steps?.length ?? 0;
								const stepTag = stepCount > 0 ? ` [→${stepCount} steps]` : "";
								return `  ${st.status} ${st.id.slice(0, 8)}: ${st.description.slice(0, 60)}${stepTag}`;
							})
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
						// ponytail: F30 — gRPC CancelTaskRequest has NO subtask field
						// (the server ignores it and cancels the whole task), but the
						// old code passed subtask_id through and then reported the
						// *subtask* as cancelled — actively wrong. Route subtask-level
						// cancel through the local orchestrator, which does real
						// subtask cancel + cascade and returns a discriminated outcome.
						if (p.subtask_id && orchestrator) {
							const r = await orchestrator.cancelTask(p.task_id, p.subtask_id);
							return {
								content: [{
									type: "text" as const,
									text: r.ok
										? `Cancelled subtask ${p.subtask_id} in task ${r.taskId} (cascade applied)`
										: `Cancel failed (${r.reason})${r.candidates?.length ? `: ${r.candidates.join(", ")}` : ""}`,
								}],
							};
						}
						// ponytail: F40 — down server must read as "unavailable", not
						// "not found"; tasks the local orchestrator owns still cancel.
						if (!bridge.isConnected()) {
							if (orchestrator) {
								const r = await orchestrator.cancelTask(p.task_id);
								return {
									content: [{
										type: "text" as const,
										text: r.ok
											? `Cancelled task ${r.taskId} (local — gRPC server unavailable)`
											: localFailureMessage("Cancel", p.task_id, r),
									}],
									...(r.ok ? {} : { isError: true }),
								};
							}
							return {
								content: [{ type: "text" as const, text: "Cancel failed: gRPC server unavailable" }],
								isError: true,
							};
						}
						// Whole-task cancel stays server-side (remote tasks aren't in
						// the local orchestrator). Don't pass subtask_id — the server
						// ignores it; claiming subtask success here would lie.
						const ok = await bridge.cancelTask(p.task_id);
						return {
							content: [{
								type: "text" as const,
								text: ok
									? `Cancelled task ${p.task_id}${p.subtask_id ? " (server-side whole-task cancel; subtask-level cancel needs the local orchestrator)" : ""}`
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
						// ponytail: F40 — bridge collapses transport failure to false,
						// which used to read as "not in progress" when the server was
						// down. Check connectivity; fall back to the local orchestrator.
						if (!bridge.isConnected()) {
							if (!orchestrator) {
								return { content: [{ type: "text" as const, text: "Pause failed: gRPC server unavailable" }], isError: true };
							}
							const r = await orchestrator.pauseTask(p.task_id);
							return {
								content: [{
									type: "text" as const,
									text: r.ok
										? `Paused task ${r.taskId} (local — gRPC server unavailable)`
										: localFailureMessage("Pause", p.task_id, r),
								}],
								...(r.ok ? {} : { isError: true }),
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
						// ponytail: F40 — same connectivity guard as pause.
						if (!bridge.isConnected()) {
							if (!orchestrator) {
								return { content: [{ type: "text" as const, text: "Resume failed: gRPC server unavailable" }], isError: true };
							}
							const r = await orchestrator.resumeTask(p.task_id);
							return {
								content: [{
									type: "text" as const,
									text: r.ok
										? `Resumed task ${r.taskId} (local — gRPC server unavailable)`
										: localFailureMessage("Resume", p.task_id, r),
								}],
								...(r.ok ? {} : { isError: true }),
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
							// ponytail: F40 — with the server down, "not found" is
							// wrong: consult the local orchestrator, and only report
							// unavailability if the task isn't local either.
							if (!bridge.isConnected()) {
								if (orchestrator) {
									const resolved = orchestrator.resolveTask(p.task_id);
									if (!("ok" in resolved)) {
										return { content: [{ type: "text" as const, text: renderLocalTask(resolved) }] };
									}
								}
								return {
									content: [{
										type: "text" as const,
										text: `Status unavailable: gRPC server is down${orchestrator ? ` and no local task matches "${p.task_id}"` : ""}`,
									}],
									isError: true,
								};
							}
							const task = await bridge.getTask(p.task_id);
							if (!task) {
								return {
									content: [{ type: "text" as const, text: `Task ${p.task_id} not found` }],
									useless: true,
								};
							}
							const subtaskLines = task.subtasks
								.map((st) => {
									const stepCount = st.steps?.length ?? 0;
									const stepTag = stepCount > 0 ? ` [→${stepCount} steps]` : "";
									return `  [${st.status}] ${st.id.slice(0, 8)}: ${st.description.slice(0, 60)}${stepTag}`;
								})
								.join("\n");
							return {
								content: [{
									type: "text" as const,
									text: `Task ${task.taskId}\nStatus: ${task.status}\nProject: ${task.projectId}\nSubtasks:\n${subtaskLines || "  (none)"}`,
								}],
							};
						}
						// List all tasks
						// ponytail: F40 — "(no tasks)" on a down server made LLMs
						// think nothing existed. Show the local view with a banner.
						if (!bridge.isConnected() && orchestrator) {
							const local = orchestrator.getAllTaskStates();
							const localLines = local
								.slice(0, 20)
								.map((t) => `[${t.status}] ${t.id.slice(0, 8)}: ${t.description.slice(0, 60)}`);
							return {
								content: [{
									type: "text" as const,
									text: `${localLines.join("\n") || "(no local tasks)"}\n(local view — gRPC server unavailable; remote tasks not shown)`,
								}],
							};
						}
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
