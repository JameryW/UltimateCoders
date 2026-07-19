/**
 * Memory Bridge — registers uc_memory and uc_search tools with omp ExtensionAPI.
 *
 * Routes tool calls through GrpcBridge to the UC Rust engine:
 * - uc_memory: read/write/search/delete UC layered memory (TiKV + Qdrant + PostgreSQL)
 * - uc_search: hybrid code search (text + semantic + AST) with repo/language/path filtering
 *
 * Extracted from extension.ts for modularity.
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
		action: pi.zod.enum(["read", "write", "search", "delete"]).describe("Memory operation"),
		scope: pi.zod.string().describe("Memory scope: short_term, long_term, metadata (or task/project/global)"),
		// ponytail: F35 — for action=search this field IS the query; saying only
		// "Memory key" made LLMs pass a literal key and get "(no results)".
		key: pi.zod.string().describe("Memory key (or the search query when action=search)"),
		content: pi.zod.string().optional().describe("Content to write (required for write action)"),
		content_type: pi.zod.enum(["text", "structured", "code", "diff", "reference"]).optional().describe("Content type for write (default: text)"),
		importance: pi.zod.number().min(0).max(1).optional().describe("Importance score 0-1 for write (>= 0.7 writes to long-term memory)"),
		tags: pi.zod.array(pi.zod.string()).optional().describe("Tags for write (categorization)"),
	});

	pi.registerTool({
		name: "uc_memory",
		label: "UC Memory",
		description:
			"Read/write/search/delete UltimateCoders layered memory. " +
			"Scopes: short_term=task(TiKV), long_term=global(Qdrant), metadata=project(PostgreSQL). " +
			"Also accepts task/project/global.",
		parameters: memorySchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as { action: string; scope: string; key: string; content?: string; content_type?: string; importance?: number; tags?: string[] };
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
					const ok = await bridge.writeMemory(
						mapScope(p.scope), p.key, p.content,
						p.content_type ?? "text",
						"uc-orchestrator",
						"", "",
						p.importance,
						p.tags,
					);
					// ponytail: F35 — the bridge collapses failure to a bool (the
					// server error only lands in a console.warn); say what we can.
					return {
						content: [{
							type: "text" as const,
							text: ok ? "Written successfully" : "Write failed (server rejected the entry or is unavailable)",
						}],
						...(ok ? {} : { isError: true }),
					};
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
				if (p.action === "delete") {
					const ok = await bridge.deleteMemory(mapScope(p.scope), p.key);
					return {
						content: [{
							type: "text" as const,
							text: ok ? `Deleted key ${p.key} from ${p.scope}` : `Delete failed for key ${p.key}`,
						}],
					};
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
		repo_ids: pi.zod.array(pi.zod.string()).optional().describe("Filter to specific repository IDs"),
		languages: pi.zod.array(pi.zod.string()).optional().describe("Filter by programming languages (e.g. ['typescript','rust'])"),
		path_patterns: pi.zod.array(pi.zod.string()).optional().describe("Filter by path patterns (e.g. ['src/**/*.ts'])"),
	});

	pi.registerTool({
		name: "uc_search",
		label: "UC Search",
		description:
			"Search UltimateCoders hybrid index (text + semantic + AST). " +
			"Supports filtering by repo, language, and path patterns. " +
			"Routes through UC Rust engine via gRPC.",
		parameters: searchSchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as { query: string; modes?: string[]; max_results?: number; repo_ids?: string[]; languages?: string[]; path_patterns?: string[] };
			try {
				const results = await bridge.searchCode(
					p.query, p.modes, p.max_results,
					p.repo_ids, p.languages, p.path_patterns,
				);
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
