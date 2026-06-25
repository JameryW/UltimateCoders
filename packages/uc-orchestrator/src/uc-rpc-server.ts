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
 *   submit_task   → {description: string} → {task_id: string}
 *   cancel_task   → {task_id: string, subtask_id?: string} → {ok: boolean}
 *   pause_task    → {task_id: string} → {ok: boolean}
 *   resume_task   → {task_id: string} → {ok: boolean}
 *   show_status   → {task_id?: string} → {status: object}
 *   shutdown      → {} → {}
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
		this.output({ event: "notify", data: { message, type } });
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
