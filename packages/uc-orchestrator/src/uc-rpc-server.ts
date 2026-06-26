/**
 * uc-rpc-server.ts -- JSONL stdio bridge for Python OmpBridge.
 *
 * Reads JSONL commands from stdin, dispatches to UCOrchestrator,
 * writes JSONL responses + events to stdout.
 *
 * Protocol:
 *   Request:  {"method": "<name>", "params": {...}, "id": <int>}
 *   Response: {"id": <int>, "result": {...}} or {"id": <int>, "error": "<msg>"}
 *   Event:    {"event": "<type>", "data": {...}}
 *   Startup:  {"event": "ready"}
 *
 * Usage: bun run packages/uc-orchestrator/src/uc-rpc-server.ts
 */

import * as readline from "node:readline";
import { UCOrchestrator, type TaskState } from "./orchestrator/orchestrator";
import { GrpcBridge } from "./orchestrator/grpc-bridge";
import type { ExtensionAPI, ExtensionCommandContext } from "@oh-my-pi/pi-coding-agent";
import type { OrchestratorEventType } from "./orchestrator/events";

// -- Types -------------------------------------------------------------------

interface JsonRpcRequest {
	jsonrpc?: string;
	method: string;
	params: Record<string, unknown>;
	id?: number;
}

interface JsonRpcResponse {
	id: number;
	result?: unknown;
	error?: { code: number; message: string };
}

interface JsonRpcEvent {
	event: string;
	data: unknown;
}

// -- Stub ExtensionAPI -------------------------------------------------------

// Minimal stub: logger to stderr, settings with workspaceRoot
const stubPi: ExtensionAPI = {
	pi: { settings: { workspaceRoot: process.cwd() } },
	logger: {
		info: (msg: string) => process.stderr.write(`[info] ${msg}\n`),
		warn: (msg: string) => process.stderr.write(`[warn] ${msg}\n`),
		error: (msg: string) => process.stderr.write(`[error] ${msg}\n`),
	},
	sendMessage: () => {},
} as unknown as ExtensionAPI;

// Stub ctx for orchestrator methods that need it
function stubCtx(): ExtensionCommandContext {
	return {
		cwd: process.cwd(),
		ui: {
			notify: () => {},
			setWidget: () => {},
		},
	} as unknown as ExtensionCommandContext;
}

// -- Serialize helpers -------------------------------------------------------

function serializeTask(task: TaskState): Record<string, unknown> {
	return {
		id: task.id,
		description: task.description,
		status: task.status,
		controlState: task.controlState,
		createdAt: task.createdAt,
		completedAt: task.completedAt,
		error: task.error,
		subtasks: task.subtasks.map((st) => ({
			id: st.id,
			description: st.description,
			status: st.status,
			dependsOn: st.dependsOn,
			result: st.result,
			error: st.error,
		})),
	};
}

// -- Server ------------------------------------------------------------------

class RpcServer {
	private orchestrator: UCOrchestrator;
	private nextId = 0;

	constructor() {
		const bridge = new GrpcBridge();
		this.orchestrator = new UCOrchestrator(stubPi, undefined, bridge);
	}

	async init(): Promise<void> {
		await this.orchestrator.restore();
		// Subscribe orchestrator events → JSONL stdout for Python bridge
		const eventTypes: OrchestratorEventType[] = [
			"task_planning", "task_decomposed", "task_complete",
			"task_paused", "task_resumed", "task_cancelled",
			"wave_start", "wave_end",
			"subtask_start", "subtask_end", "subtask_failed", "subtask_reviewing",
		];
		for (const type of eventTypes) {
			this.orchestrator.events.on(type, (data) => {
				this.emitEvent(type, data);
			});
		}
	}

	async dispatch(req: JsonRpcRequest): Promise<JsonRpcResponse> {
		const id = req.id ?? ++this.nextId;
		try {
			const result = await this.handleMethod(req.method, req.params);
			return { id, result };
		} catch (err) {
			return {
				id,
				error: {
					code: -32000,
					message: err instanceof Error ? err.message : String(err),
				},
			};
		}
	}

	private async handleMethod(
		method: string,
		params: Record<string, unknown>,
	): Promise<unknown> {
		switch (method) {
			case "submit_task": {
				const description = String(params.description ?? "");
				if (!description) throw new Error("description is required");
				// Fire-and-forget: submitTask blocks until decomposition + execution
				// complete, but the RPC protocol requires an immediate task_id response.
				// Generate the task ID synchronously, return it, then run the
				// orchestrator lifecycle in the background.
				const taskId = this.orchestrator.createTask(description);
				this.orchestrator
					.runTask(taskId, stubCtx())
					.catch((err) => {
						this.emitEvent("task_error", {
							task_id: taskId,
							error: err instanceof Error ? err.message : String(err),
						});
					});
				return { task_id: taskId };
			}

			case "cancel_task": {
				const taskId = String(params.task_id ?? "");
				if (!taskId) throw new Error("task_id is required");
				const ok = await this.orchestrator.cancelTask(taskId, params.subtask_id as string | undefined);
				return { ok };
			}

			case "pause_task": {
				const taskId = String(params.task_id ?? "");
				if (!taskId) throw new Error("task_id is required");
				const ok = await this.orchestrator.pauseTask(taskId);
				return { ok };
			}

			case "resume_task": {
				const taskId = String(params.task_id ?? "");
				if (!taskId) throw new Error("task_id is required");
				const ok = await this.orchestrator.resumeTask(taskId);
				return { ok };
			}

			case "show_status": {
				const taskId = params.task_id ? String(params.task_id) : undefined;
				if (!taskId) {
					// List all tasks summary
					const tasks = this.orchestrator.getAllTaskStates();
					return {
						status: "ok",
						tasks: tasks.map((t) => ({
							id: t.id,
							status: t.status,
							controlState: t.controlState,
							description: t.description.slice(0, 60),
							subtaskProgress: `${t.subtasks.filter((s) => s.status === "completed").length}/${t.subtasks.length}`,
						})),
					};
				}
				const task = this.orchestrator.getTaskState(taskId);
				if (!task) return { status: "not_found" };
				return { status: "ok", task: serializeTask(task) };
			}

			case "get_task": {
				const taskId = String(params.task_id ?? "");
				if (!taskId) throw new Error("task_id is required");
				const task = this.orchestrator.getTaskState(taskId);
				if (!task) return { task: null };
				return { task: serializeTask(task) };
			}

			case "list_tasks": {
				const tasks = this.orchestrator.getAllTaskStates();
				return { tasks: tasks.map(serializeTask) };
			}

			case "shutdown": {
				// Schedule exit after response is written
				setImmediate(() => {
					process.exit(0);
				});
				return { ok: true };
			}

			default:
				throw new Error(`Unknown method: ${method}`);
		}
	}

	private emitEvent(type: string, data: unknown): void {
		writeLine(JSON.stringify({ event: type, data }));
	}
}

// -- Main --------------------------------------------------------------------

async function main(): Promise<void> {
	const server = new RpcServer();
	await server.init();

	const rl = readline.createInterface({ input: process.stdin });

	// Write ready signal
	writeLine(JSON.stringify({ event: "ready" }));

	rl.on("line", async (line) => {
		const trimmed = line.trim();
		if (!trimmed) return;

		let req: JsonRpcRequest;
		try {
			req = JSON.parse(trimmed);
		} catch {
			writeLine(JSON.stringify({
				id: 0,
				error: { code: -32700, message: "Parse error" },
			}));
			return;
		}

		const resp = await server.dispatch(req);
		writeLine(JSON.stringify(resp));
	});

	rl.on("close", () => {
		process.exit(0);
	});
}

function writeLine(s: string): void {
	process.stdout.write(s + "\n");
}

main().catch((err) => {
	process.stderr.write(`Fatal: ${err}\n`);
	process.exit(1);
});
