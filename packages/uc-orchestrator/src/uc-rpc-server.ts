/**
 * UC RPC Server — Standalone JSONL stdio server for UCOrchestrator.
 *
 * Runs outside the omp host process. Python bridge spawns this as a subprocess,
 * sends JSONL commands on stdin, receives JSONL responses on stdout.
 *
 * Protocol:
 *   Command:  {"method": "<name>", "params": {...}, "id": <number>}
 *   Response: {"id": <number>, "result": {...}} | {"id": <number>, "error": "<message>"}
 *   Event:    {"event": "<type>", "data": {...}}
 *
 * Methods:
 *   submit_task   → {description: string} → {ok: boolean}
 *   cancel_task   → {task_id: string, subtask_id?: string} → {ok: boolean}
 *   pause_task    → {task_id: string} → {ok: boolean}
 *   resume_task   → {task_id: string} → {ok: boolean}
 *   show_status   → {task_id?: string} → {status: object}
 *   get_task      → {task_id: string} → {task: object}
 *   list_tasks    → {} → {tasks: object[]}
 *   shutdown      → {} → {}
 *
 * Events (async, streamed to stdout):
 *   task_submitted, subtask_started, subtask_completed, subtask_failed, task_completed
 */
 */

import { readJsonl } from "@oh-my-pi/pi-utils";
import { UCOrchestrator } from "./orchestrator/orchestrator";
import { GrpcBridge } from "./orchestrator/grpc-bridge";
import type { ExtensionAPI, ExtensionCommandContext } from "@oh-my-pi/pi-coding-agent";

// ── Stub UI Context ──────────────────────────────────────────────
// ponytail: minimal stub — only notify/setWidget used by UCOrchestrator

class RpcUIContext {
	private output: (obj: object) => void;

	constructor(output: (obj: object) => void) {
		this.output = output;
	}

	notify(message: string, type?: "info" | "warning" | "error"): void {
		// Emit both raw notify and parsed structured events for TUI/Dashboard
		this.output({ event: "notify", data: { message, type } });
		// ponytail: parse omp notify messages into structured events for Python event_emitter
		this._parseStructuredEvent(message);
	}

	/** Parse omp's notify messages into structured events for Python TUI. */
	private _parseStructuredEvent(message: string): void {
		// "Task uc-1-xxx: planning..."
		const taskPlanMatch = message.match(/^Task (uc-\S+): planning/);
		if (taskPlanMatch) {
			this.output({ event: "task_submitted", data: { task_id: taskPlanMatch[1] } });
			return;
		}
		// "Task uc-1-xxx: 3 subtasks, 2 wave(s)"
		const decompMatch = message.match(/^Task (uc-\S+): (\d+) subtasks, (\d+) wave/);
		if (decompMatch) {
			this.output({ event: "task_decomposed", data: {
				task_id: decompMatch[1],
				subtask_count: parseInt(decompMatch[2]),
				wave_count: parseInt(decompMatch[3]),
			} });
			return;
		}
		// "Task uc-1-xxx: wave 1/2 — [st-1, st-2]"
		const waveMatch = message.match(/^Task (uc-\S+): wave (\d+)\/(\d+) — \[(.+?)\]/);
		if (waveMatch) {
			const subtaskIds = waveMatch[4].split(", ").map(s => s.trim());
			this.output({ event: "wave_started", data: {
				task_id: waveMatch[1],
				wave: parseInt(waveMatch[2]),
				total_waves: parseInt(waveMatch[3]),
				subtask_ids: subtaskIds,
			} });
			for (const stId of subtaskIds) {
				this.output({ event: "subtask_started", data: { task_id: waveMatch[1], subtask_id: stId } });
			}
			return;
		}
		// "Task uc-1-xxx: completed" / "Task uc-1-xxx: failed"
		const doneMatch = message.match(/^Task (uc-\S+): (completed|failed|cancelled)/);
		if (doneMatch) {
			this.output({ event: "task_completed", data: { task_id: doneMatch[1], status: doneMatch[2] } });
			return;
		}
	}

	setWidget(_key: string, _content: unknown): void {
		// ponytail: no-op in RPC mode — Python bridge doesn't render widgets
	}

	setStatus(_key: string, _text: string | undefined): void {}
	setWorkingMessage(_message?: string): void {}
	setTitle(_title: string): void {}
	setEditorText(_text: string): void {}
	pasteToEditor(_text: string): void {}
	getEditorText(): string { return ""; }
	setFooter(_factory: unknown): void {}
	setHeader(_factory: unknown): void {}
	setEditorComponent(): void {}
	getToolsExpanded(): boolean { return false; }
	setToolsExpanded(_expanded: boolean): void {}
	onTerminalInput(): () => void { return () => {}; }

	async select(_title: string, _options: string[]): Promise<string | undefined> { return undefined; }
	async confirm(_title: string, _message: string): Promise<boolean> { return false; }
	async input(_title: string, _placeholder?: string): Promise<string | undefined> { return undefined; }
	async editor(_title: string, _prefill?: string): Promise<string | undefined> { return undefined; }
	async custom<T>(): Promise<T> { return undefined as T; }

	get theme() { return {} as any; }
	async getAllThemes() { return []; }
	async getTheme(_name: string) { return undefined; }
	async setTheme(_theme: string | object) { return { success: false, error: "Not supported in RPC mode" }; }
}

// ── Stub Command Context ─────────────────────────────────────────

function makeCommandContext(output: (obj: object) => void): ExtensionCommandContext {
	const ui = new RpcUIContext(output) as any;
	const ctx: unknown = {
		ui,
		cwd: process.cwd(),
		hasUI: false,
		models: [],
		getContextUsage: () => undefined,
		compact: async () => {},
		isIdle: () => true,
		abort: () => {},
		hasPendingMessages: () => false,
		shutdown: () => process.exit(0),
		getSystemPrompt: () => "",
		sessionManager: {} as any,
		modelRegistry: {} as any,
		model: undefined,
		searchDb: undefined,
		waitForIdle: async () => {},
		newSession: async () => ({ cancelled: false }),
		branch: async () => ({ cancelled: false }),
		navigateTree: async () => ({ cancelled: false }),
		switchSession: async () => ({ cancelled: false }),
		reload: async () => {},
	};
	return ctx as ExtensionCommandContext;
}

// ── Command Types ────────────────────────────────────────────────

interface RpcCommand {
	method: string;
	params: Record<string, unknown>;
	id?: number;
}

interface RpcResult {
	id?: number;
	result?: unknown;
	error?: string;
}

// ── Serialization ───────────────────────────────────────────────

// ponytail: serialize TaskState + SubtaskResult for Python consumption
function serializeTask(task: any): object {
	return {
		id: task.id,
		description: task.description,
		status: task.status,
		control_state: task.controlState,
		created_at: task.createdAt,
		completed_at: task.completedAt ?? null,
		error: task.error ?? null,
		subtasks: (task.subtasks ?? []).map((st: any) => ({
			id: st.id,
			description: st.description,
			status: st.status,
			depends_on: st.dependsOn,
			files: st.files,
			result: st.result ?? null,
			error: st.error ?? null,
			started_at: st.startedAt ?? null,
			completed_at: st.completedAt ?? null,
			review: st.review ?? null,
			retry_count: st.retryCount ?? 0,
		})),
	};
}

// ── Main ─────────────────────────────────────────────────────────

export async function runUcRpcServer(): Promise<void> {
	const output = (obj: object) => {
		process.stdout.write(`${JSON.stringify(obj)}\n`);
	};

	// Signal ready
	output({ event: "ready" });

	// Create orchestrator with stub ExtensionAPI
	// ponytail: stub only the methods UCOrchestrator actually uses
	const stubPi = {
		logger: {
			info: (...args: unknown[]) => output({ event: "log", data: { level: "info", args } }),
			warn: (...args: unknown[]) => output({ event: "log", data: { level: "warn", args } }),
			error: (...args: unknown[]) => output({ event: "log", data: { level: "error", args } }),
			debug: () => {},
		},
		pi: { settings: {} },
		sendMessage: (msg: unknown) => output({ event: "message", data: msg }),
		// Stub remaining ExtensionAPI methods — UCOrchestrator doesn't call these directly
		on: () => {},
		registerTool: () => {},
		registerCommand: () => {},
		registerShortcut: () => {},
		registerFlag: () => {},
		setLabel: () => {},
		getFlag: () => undefined,
		registerMessageRenderer: () => {},
		sendUserMessage: () => {},
		appendEntry: () => {},
		exec: async () => ({ stdout: "", stderr: "", exitCode: 0 }),
		getActiveTools: () => [] as string[],
		getAllTools: () => [] as string[],
		setActiveTools: async () => {},
		getCommands: () => [],
		setModel: async () => false,
		getThinkingLevel: () => undefined,
		setThinkingLevel: () => {},
		registerProvider: () => {},
		events: { on: () => {}, off: () => {}, emit: async () => {} },
		typebox: {} as any,
	} as unknown as ExtensionAPI;

	const bridge = new GrpcBridge();
	const orchestrator = new UCOrchestrator(stubPi, undefined, bridge);

	// Restore persisted tasks
	try {
		await orchestrator.restore();
	} catch (err) {
		output({ event: "log", data: { level: "warn", args: [`Restore failed: ${err}`] } });
	}

	const ctx = makeCommandContext(output);

	// Handle commands
	const handleCommand = async (cmd: RpcCommand): Promise<RpcResult> => {
		const { method, params, id } = cmd;

		try {
			switch (method) {
				case "submit_task": {
					const description = params.description as string;
					if (!description) return { id, error: "Missing description" };
					await orchestrator.submitTask(description, ctx);
					return { id, result: { ok: true } };
				}
				case "cancel_task": {
					const taskId = params.task_id as string;
					if (!taskId) return { id, error: "Missing task_id" };
					const ok = await orchestrator.cancelTask(taskId, params.subtask_id as string | undefined, ctx);
					return { id, result: { ok } };
				}
				case "pause_task": {
					const taskId = params.task_id as string;
					if (!taskId) return { id, error: "Missing task_id" };
					const ok = await orchestrator.pauseTask(taskId, ctx);
					return { id, result: { ok } };
				}
				case "resume_task": {
					const taskId = params.task_id as string;
					if (!taskId) return { id, error: "Missing task_id" };
					const ok = await orchestrator.resumeTask(taskId, ctx);
					return { id, result: { ok } };
				}
				case "show_status": {
					await orchestrator.showStatus(params.task_id as string | undefined, ctx);
					return { id, result: { ok: true } };
				}
				case "get_task": {
					const taskId = params.task_id as string;
					if (!taskId) return { id, error: "Missing task_id" };
					const task = orchestrator.taskStates.get(taskId);
					if (!task) return { id, error: `Task ${taskId} not found` };
					return { id, result: { task: serializeTask(task) } };
				}
				case "list_tasks": {
					const tasks = Array.from(orchestrator.taskStates.values()).map(serializeTask);
					return { id, result: { tasks } };
				}
				case "shutdown": {
					output({ id, result: { ok: true } });
					process.exit(0);
				}
				default:
					return { id, error: `Unknown method: ${method}` };
			}
		} catch (err) {
			return { id, error: err instanceof Error ? err.message : String(err) };
		}
	};

	// Read JSONL from stdin
	for await (const parsed of readJsonl(Bun.stdin.stream())) {
		try {
			const cmd = parsed as RpcCommand;
			const response = await handleCommand(cmd);
			output(response);
		} catch (err) {
			output({ error: `Parse error: ${err instanceof Error ? err.message : String(err)}` });
		}
	}

	// stdin closed — client gone
	process.exit(0);
}
