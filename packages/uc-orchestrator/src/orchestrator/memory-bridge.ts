/**
 * Memory Bridge — registers uc_memory and uc_search tools with omp ExtensionAPI.
 *
 * Routes tool calls through GrpcBridge to the UC Rust engine:
 * - uc_memory: read/write/search UC layered memory (TiKV + Qdrant + PostgreSQL)
 * - uc_search: hybrid code search (text + semantic + AST)
 *
 * Extracted from extension.ts for modularity — Phase 3 cleanup.
 */

import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { GrpcBridge } from "./grpc-bridge";

// ponytail: scope mapping — omp uses short_term/long_term/metadata,
// gRPC uses task/project/global. Map here so agents can use either.
const SCOPE_MAP: Record<string, string> = {
	short_term: "task",
	long_term: "global",
	metadata: "project",
	task: "task",
	project: "project",
	global: "global",
};
function mapScope(scope: string): string {
	return SCOPE_MAP[scope] ?? scope;
}

// ponytail: `as never` on parameters to dodge TS2589 deep instantiation
// in registerTool<TParams extends TSchema>. Runtime schema is correct,
// only compile-time inference overflows. Execute callback types params manually.

export function registerMemoryTools(pi: ExtensionAPI, bridge: GrpcBridge): void {
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
			"Scopes: short_term=task(TiKV), long_term=global(Qdrant), metadata=project(PostgreSQL). Also accepts task/project/global.",
		parameters: memorySchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as { action: string; scope: string; key: string; content?: string };
			try {
				if (p.action === "read") {
					const result = await bridge.readMemory(mapScope(p.scope), p.key);
					if (result === null) {
						return { content: [{ type: "text" as const, text: "(no memory found)" }], useless: true };
					}
					return { content: [{ type: "text" as const, text: result }] };
				}
				if (p.action === "write") {
					if (!p.content) {
						return {
							content: [{ type: "text" as const, text: "Error: content required for write action" }],
							isError: true,
						};
					}
					const ok = await bridge.writeMemory(mapScope(p.scope), p.key, p.content);
					return { content: [{ type: "text" as const, text: ok ? "Written successfully" : "Write failed" }] };
				}
				if (p.action === "search") {
					const results = await bridge.searchMemory(p.key, mapScope(p.scope));
					if (results.length === 0) {
						return { content: [{ type: "text" as const, text: "(no results)" }], useless: true };
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
					content: [{
						type: "text" as const,
						text: `UC Memory error: ${err instanceof Error ? err.message : String(err)}`,
					}],
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
					return { content: [{ type: "text" as const, text: "(no results)" }], useless: true };
				}
				const lines = results.map(
					(r) => `${r.filePath} (score=${r.score.toFixed(2)}): ${r.snippet.slice(0, 150)}`,
				);
				return { content: [{ type: "text" as const, text: lines.join("\n") }] };
			} catch (err) {
				return {
					content: [{
						type: "text" as const,
						text: `UC Search error: ${err instanceof Error ? err.message : String(err)}`,
					}],
					isError: true,
				};
			}
		},
	});
}
