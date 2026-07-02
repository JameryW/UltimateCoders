/**
 * Index Bridge — registers uc_index tool with omp ExtensionAPI.
 *
 * Wraps index management operations (index_repo/list_repos/get_state/remove_index)
 * as an LLM-callable tool so agents can manage code indexes autonomously.
 */

import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { GrpcBridge } from "./grpc-bridge";

export function registerIndexTools(pi: ExtensionAPI, bridge: GrpcBridge): void {
	const indexSchema = pi.zod.object({
		action: pi.zod.enum(["index_repo", "list_repos", "get_state", "remove_index"]).describe("Index action"),
		repo_id: pi.zod.string().optional().describe("Repository ID (for index_repo/get_state/remove_index)"),
		local_path: pi.zod.string().optional().describe("Local path to repo (for index_repo)"),
		languages: pi.zod.array(pi.zod.string()).optional().describe("Languages to index (for index_repo, e.g. ['typescript','rust'])"),
		workspace_id: pi.zod.string().optional().describe("Workspace ID to scope list_repos to, or to assign the repo to for index_repo (default: 'default')"),
	});

	pi.registerTool({
		name: "uc_index",
		label: "UC Index",
		description:
			"Manage UltimateCoders code indexes. " +
			"Index a repository (text + semantic + AST), list indexed repos, " +
			"check index state, or remove an index.",
		parameters: indexSchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as { action: string; repo_id?: string; local_path?: string; languages?: string[]; workspace_id?: string };
			try {
				switch (p.action) {
					case "index_repo": {
						if (!p.repo_id || !p.local_path) {
							return {
								content: [{ type: "text" as const, text: "Error: repo_id and local_path required for index_repo" }],
								isError: true,
							};
						}
						const ok = await bridge.indexRepo(p.repo_id, p.local_path, p.languages, p.workspace_id ?? "default");
						return {
							content: [{
								type: "text" as const,
								text: ok ? `Indexing started for ${p.repo_id} at ${p.local_path}` : `Indexing failed for ${p.repo_id}`,
							}],
						};
					}
					case "list_repos": {
						const repos = await bridge.listRepos(p.workspace_id);
						if (repos.length === 0) {
							return { content: [{ type: "text" as const, text: "(no indexed repos)" }], useless: true };
						}
						const lines = repos.map((r) => `${r.repoId} [${r.status}] ${r.indexedFiles} files (workspace: ${r.workspaceId})`);
						return { content: [{ type: "text" as const, text: lines.join("\n") }] };
					}
					case "get_state": {
						if (!p.repo_id) {
							return {
								content: [{ type: "text" as const, text: "Error: repo_id required for get_state" }],
								isError: true,
							};
						}
						const state = await bridge.getIndexState(p.repo_id);
						if (!state) {
							return {
								content: [{ type: "text" as const, text: `No index found for ${p.repo_id}` }],
								useless: true,
							};
						}
						return {
							content: [{
								type: "text" as const,
								text: `Repo: ${p.repo_id}\nStatus: ${state.status}\nIndexed files: ${state.indexedFiles}\nLast indexed: ${state.lastIndexed}`,
							}],
						};
					}
					case "remove_index": {
						if (!p.repo_id) {
							return {
								content: [{ type: "text" as const, text: "Error: repo_id required for remove_index" }],
								isError: true,
							};
						}
						const ok = await bridge.removeIndex(p.repo_id);
						return {
							content: [{
								type: "text" as const,
								text: ok ? `Removed index for ${p.repo_id}` : `Remove failed for ${p.repo_id}`,
							}],
						};
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
						text: `UC Index error: ${err instanceof Error ? err.message : String(err)}`,
					}],
					isError: true,
				};
			}
		},
	});
}
