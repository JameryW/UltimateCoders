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
import { registerMemoryTools } from "./orchestrator/memory-bridge";

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
						ctx.ui.notify(`Resume failed: task ${taskId} not found or not paused/failed`, "error");
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
							"  /uc resume <task-id>           Resume a paused or failed task",
							"  /uc help                       Show this help",
						].join("\n"),
						"info",
					);
					return;
			}
		},
	});

	// ── LLM-callable tools ─────────────────────────────────────
	registerMemoryTools(pi, bridge);
}
