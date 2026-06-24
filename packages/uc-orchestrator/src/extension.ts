/**
 * UC Orchestrator Extension — Task orchestration for UltimateCoders.
 *
 * Registers:
 * - /uc submit <description>     — Submit a task for orchestration
 * - /uc status [task-id]         — Check task status
 * - /uc cancel <task-id> [<st>]  — Cancel task or specific subtask
 * - /uc pause <task-id>          — Pause task after current wave
 * - /uc resume <task-id>         — Resume a paused task
 * - /uc help                     — Show help
 *
 * LLM-callable tools:
 * - uc_memory  — Read/write/search UC layered memory
 * - uc_search  — Search UC hybrid index (text + semantic + AST)
 *
 * Uses omp's agent runtime (runSubprocess, agent definitions) as the
 * execution layer. UC provides scheduling strategy, memory bridge,
 * and gRPC integration with the Rust core engine.
 */

import type { ExtensionAPI, ExtensionCommandContext } from "@oh-my-pi/pi-coding-agent";
import { UCOrchestrator } from "./orchestrator/orchestrator";
import { GrpcBridge } from "./orchestrator/grpc-bridge";

export default function ucOrchestratorExtension(pi: ExtensionAPI): void {
	pi.setLabel("UC Orchestrator");

	const bridge = new GrpcBridge();
	const orchestrator = new UCOrchestrator(pi, undefined, bridge);

	// Restore persisted tasks on startup
	orchestrator.restore().catch((err) => {
		pi.logger.warn(`Failed to restore tasks: ${err}`);
	});

	const SUBCOMMANDS = ["submit", "status", "cancel", "pause", "resume", "help"];

	pi.registerCommand("uc", {
		description: "UltimateCoders task orchestration",
		getArgumentCompletions: (prefix: string) => {
			if (!prefix) return SUBCOMMANDS.map((s) => ({ label: s, value: s }));
			return SUBCOMMANDS
				.filter((s) => s.startsWith(prefix))
				.map((s) => ({ label: s, value: s }));
		},
		handler: async (args: string, ctx: ExtensionCommandContext) => {
			const parts = args.trim().split(/\s+/);
			const subcommand = parts[0] ?? "help";
			const rest = parts.slice(1).join(" ");

			switch (subcommand) {
				case "submit": {
					if (!rest) {
						ctx.ui.notify("Usage: /uc submit <task description>", "error");
						return;
					}
					await orchestrator.submitTask(rest, ctx);
					return;
				}
				case "status": {
					await orchestrator.showStatus(rest || undefined, ctx);
					return;
				}
				case "cancel": {
					// /uc cancel <task-id> [<subtask-id>]
					const cancelParts = rest.trim().split(/\s+/);
					const taskId = cancelParts[0];
					const subtaskId = cancelParts[1];
					if (!taskId) {
						ctx.ui.notify("Usage: /uc cancel <task-id> [<subtask-id>]", "error");
						return;
					}
					const ok = await orchestrator.cancelTask(taskId, subtaskId, ctx);
					if (!ok) {
						ctx.ui.notify(`Cancel failed: task ${taskId} not found`, "error");
					}
					return;
				}
				case "pause": {
					const taskId = rest.trim();
					if (!taskId) {
						ctx.ui.notify("Usage: /uc pause <task-id>", "error");
						return;
					}
					const ok = await orchestrator.pauseTask(taskId, ctx);
					if (!ok) {
						ctx.ui.notify(`Pause failed: task ${taskId} not found or not in progress`, "error");
					}
					return;
				}
				case "resume": {
					const taskId = rest.trim();
					if (!taskId) {
						ctx.ui.notify("Usage: /uc resume <task-id>", "error");
						return;
					}
					const ok = await orchestrator.resumeTask(taskId, ctx);
					if (!ok) {
						ctx.ui.notify(`Resume failed: task ${taskId} not found or not paused`, "error");
					}
					return;
				}
				default:
					ctx.ui.notify(
						[
							"UC Orchestrator — distributed AI coding orchestration",
							"",
							"  /uc submit <description>       Submit a task",
							"  /uc status [task-id]           Check task status",
							"  /uc cancel <task-id> [<st-id>] Cancel task or subtask",
							"  /uc pause <task-id>            Pause after current wave",
							"  /uc resume <task-id>           Resume a paused task",
							"  /uc help                       Show this help",
						].join("\n"),
						"info",
					);
					return;
			}
		},
	});

	// ── LLM-callable tools ─────────────────────────────────────
	// ponytail: `as never` on parameters to dodge TS2589 deep instantiation
	// in registerTool<TParams extends TSchema> — runtime schema is correct,
	// only the compile-time inference overflows. Execute callback types params manually.

	const memorySchema = pi.zod.object({
		action: pi.zod.enum(["read", "write", "search"]).describe("Memory operation"),
		scope: pi.zod.string().describe("Memory scope: short_term, long_term, metadata"),
		key: pi.zod.string().describe("Memory key"),
		content: pi.zod.string().optional().describe("Content to write (required for write action)"),
	});

	pi.registerTool({
		name: "uc_memory",
		label: "UC Memory",
		description:
			"Read/write UltimateCoders layered memory. " +
			"Short-term (TiKV), long-term (Qdrant semantic), metadata (PostgreSQL).",
		parameters: memorySchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as { action: string; scope: string; key: string; content?: string };
			try {
				if (p.action === "read") {
					const result = await bridge.readMemory(p.scope, p.key);
					if (result === null) {
						return {
							content: [{ type: "text" as const, text: "(no memory found)" }],
							useless: true,
						};
					}
					return {
						content: [{ type: "text" as const, text: result }],
					};
				}
				if (p.action === "write") {
					if (!p.content) {
						return {
							content: [{ type: "text" as const, text: "Error: content required for write action" }],
							isError: true,
						};
					}
					const ok = await bridge.writeMemory(p.scope, p.key, p.content);
					return {
						content: [{ type: "text" as const, text: ok ? "Written successfully" : "Write failed" }],
					};
				}
				if (p.action === "search") {
					const results = await bridge.searchMemory(p.key);
					if (results.length === 0) {
						return {
							content: [{ type: "text" as const, text: "(no results)" }],
							useless: true,
						};
					}
					const lines = results.map(
						(r) => `[${r.score.toFixed(2)}] ${r.content.slice(0, 200)}`,
					);
					return { content: [{ type: "text" as const, text: lines.join("\n") }] };
				}
				return {
					content: [{ type: "text" as const, text: `Unknown action: ${p.action}` }],
					isError: true,
				};
			} catch (err) {
				return {
					content: [{ type: "text" as const, text: `UC Memory error: ${err instanceof Error ? err.message : String(err)}` }],
					isError: true,
				};
			}
		},
	});

	const searchSchema = pi.zod.object({
		query: pi.zod.string().describe("Search query"),
		modes: pi.zod.array(pi.zod.string()).optional().describe("Search modes: text, semantic, ast, hybrid"),
		max_results: pi.zod.number().optional().describe("Max results (default 5)"),
	});

	pi.registerTool({
		name: "uc_search",
		label: "UC Search",
		description:
			"Search UltimateCoders hybrid index (text + semantic + AST). " +
			"Routes through UC Rust engine via gRPC.",
		parameters: searchSchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as { query: string; modes?: string[]; max_results?: number };
			try {
				const results = await bridge.searchCode(p.query, p.modes, p.max_results);
				if (results.length === 0) {
					return {
						content: [{ type: "text" as const, text: "(no results)" }],
						useless: true,
					};
				}
				const lines = results.map(
					(r) => `${r.filePath} (score=${r.score.toFixed(2)}): ${r.snippet.slice(0, 150)}`,
				);
				return { content: [{ type: "text" as const, text: lines.join("\n") }] };
			} catch (err) {
				return {
					content: [{ type: "text" as const, text: `UC Search error: ${err instanceof Error ? err.message : String(err)}` }],
					isError: true,
				};
			}
		},
	});
}
