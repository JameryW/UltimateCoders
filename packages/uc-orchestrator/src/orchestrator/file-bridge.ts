/**
 * File Bridge — registers uc_file tool with omp ExtensionAPI.
 *
 * Wraps file operations (list_dir/get_file) as an LLM-callable tool
 * so agents can browse indexed repo file trees and read files.
 */

import { statSync } from "node:fs";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { GrpcBridge } from "./grpc-bridge";

export function registerFileTools(pi: ExtensionAPI, bridge: GrpcBridge): void {
	const fileSchema = pi.zod.object({
		action: pi.zod.enum(["list_dir", "get_file"]).describe("File action"),
		path: pi.zod.string().describe("Directory path (list_dir) or file path (get_file)"),
		repo_id: pi.zod.string().optional().describe("Repository ID to scope the operation"),
	});

	pi.registerTool({
		name: "uc_file",
		label: "UC File",
		description:
			"Browse and read files from UltimateCoders indexed repositories. " +
			"List directory contents or read file content from indexed codebases.",
		parameters: fileSchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as { action: string; path: string; repo_id?: string };
			try {
				if (p.action === "list_dir") {
					const entries = await bridge.listDir(p.path, p.repo_id);
					if (entries.length === 0) {
						// ponytail: F37 — disambiguate. Without repo_id the path is
						// local: stat tells typo from genuinely-empty. Remote repos
						// can't be stat'd client-side — say so honestly.
						if (!p.repo_id) {
							try {
								if (!statSync(p.path).isDirectory()) {
									return { content: [{ type: "text" as const, text: `Not a directory: ${p.path}` }], isError: true };
								}
								return { content: [{ type: "text" as const, text: `(empty directory: ${p.path})` }], useless: true };
							} catch {
								return { content: [{ type: "text" as const, text: `Directory not found: ${p.path}` }], useless: true };
							}
						}
						return { content: [{ type: "text" as const, text: "(empty directory, or not found in remote repo)" }], useless: true };
					}
					const lines = entries.map((e) => `${e.type === "dir" ? "📁" : "📄"} ${e.name} ${e.size > 0 ? `(${e.size}B)` : ""}`);
					return { content: [{ type: "text" as const, text: lines.join("\n") }] };
				}
				if (p.action === "get_file") {
					const content = await bridge.getFile(p.path, p.repo_id);
					if (content === null) {
						return { content: [{ type: "text" as const, text: `File not found: ${p.path}` }], useless: true };
					}
					// ponytail: truncate large files — agent should use uc_search for targeted reads
					const truncated = content.length > 8000 ? content.slice(0, 8000) + "\n... (truncated)" : content;
					return { content: [{ type: "text" as const, text: truncated }] };
				}
				return {
					content: [{ type: "text" as const, text: `Unknown action: ${p.action}` }],
					isError: true,
				};
			} catch (err) {
				return {
					content: [{
						type: "text" as const,
						text: `UC File error: ${err instanceof Error ? err.message : String(err)}`,
					}],
					isError: true,
				};
			}
		},
	});
}
