/**
 * DeepSeek Web Bridge — registers uc_deepseek_web_search / uc_deepseek_web_fetch
 * with the omp ExtensionAPI when DEEPSEEK_API_KEY is present.
 *
 * HTTP lives in UC Python (`ultimate_coders.agent.deepseek_web`); this file only
 * shells out to that module. Do not reimplement Search/Fetch in TypeScript.
 */

import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

const PYTHON_MODULE = "ultimate_coders.agent.deepseek_web";

export function isDeepSeekWebEnabled(): boolean {
	const key = process.env.DEEPSEEK_API_KEY;
	return typeof key === "string" && key.trim().length > 0;
}

function pythonExecutable(): string {
	return process.env.UC_PYTHON?.trim() || process.env.PYTHON?.trim() || "python3";
}

export interface DeepSeekWebCliResult {
	ok: boolean;
	text?: string;
	error?: string;
	status_code?: number | null;
	configured?: boolean;
}

/** Invoke the UC Python DeepSeek web CLI; returns parsed JSON stdout. */
export async function invokeDeepSeekWebCli(
	args: string[],
	opts?: { env?: NodeJS.ProcessEnv; python?: string; timeoutMs?: number },
): Promise<DeepSeekWebCliResult> {
	const python = opts?.python ?? pythonExecutable();
	const timeoutMs = opts?.timeoutMs ?? 90_000;
	const env = opts?.env ?? process.env;

	return new Promise((resolve) => {
		const child = spawn(python, ["-m", PYTHON_MODULE, ...args], {
			env,
			stdio: ["ignore", "pipe", "pipe"],
		});
		let stdout = "";
		let stderr = "";
		let settled = false;
		const timer = setTimeout(() => {
			if (settled) return;
			settled = true;
			child.kill("SIGKILL");
			resolve({ ok: false, error: `DeepSeek web CLI timed out after ${timeoutMs}ms` });
		}, timeoutMs);

		child.stdout.setEncoding("utf8");
		child.stderr.setEncoding("utf8");
		child.stdout.on("data", (chunk: string) => {
			stdout += chunk;
		});
		child.stderr.on("data", (chunk: string) => {
			stderr += chunk;
		});
		child.on("error", (err) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			resolve({
				ok: false,
				error:
					`Failed to start DeepSeek web CLI (${python}): ${err.message}. ` +
					"Ensure the UltimateCoders Python package is installed (UC_PYTHON).",
			});
		});
		child.on("close", () => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			const line = stdout.trim().split("\n").filter(Boolean).at(-1) ?? "";
			if (!line) {
				resolve({
					ok: false,
					error:
						stderr.trim() ||
						"DeepSeek web CLI produced no JSON output",
				});
				return;
			}
			try {
				const parsed = JSON.parse(line) as DeepSeekWebCliResult;
				if (typeof parsed.ok !== "boolean") {
					resolve({ ok: false, error: "DeepSeek web CLI returned invalid JSON" });
					return;
				}
				resolve(parsed);
			} catch {
				resolve({
					ok: false,
					error: `DeepSeek web CLI returned non-JSON: ${line.slice(0, 200)}`,
				});
			}
		});
	});
}

function toolError(message: string) {
	return {
		content: [{ type: "text" as const, text: message }],
		isError: true as const,
	};
}

/**
 * Register DeepSeek web tools when DEEPSEEK_API_KEY is set.
 * No-op (tools absent) when the key is missing — fail closed at call time
 * if the env is cleared after registration.
 */
export function registerDeepSeekWebTools(pi: ExtensionAPI): void {
	if (!isDeepSeekWebEnabled()) {
		return;
	}

	// ponytail: `as never` on parameters to dodge TS2589 deep instantiation
	const searchSchema = pi.zod.object({
		query: pi.zod.string().describe("Search query (1–128 chars; supports site:, quoted phrases)"),
		count: pi.zod.number().int().min(1).max(50).optional().describe("Result count 1–50 (default 10)"),
		ori_question: pi.zod.string().optional().describe("Original user question (optional)"),
		need_content: pi.zod.boolean().optional().describe("Include page content when available"),
	});

	pi.registerTool({
		name: "uc_deepseek_web_search",
		label: "DeepSeek Web Search",
		description:
			"Search the public web via DeepSeek official Search API (UC Python → POST /v1/tools/search). " +
			"Account QPS 5; $0.75 / 1k successful calls (agent-embedded grace ends 2026-11-01). " +
			"Requires DEEPSEEK_API_KEY.",
		parameters: searchSchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as {
				query: string;
				count?: number;
				ori_question?: string;
				need_content?: boolean;
			};
			try {
				if (!isDeepSeekWebEnabled()) {
					return toolError(
						"DEEPSEEK_API_KEY is not set. Set it to enable DeepSeek Search/Fetch.",
					);
				}
				const args = ["search", "--query", p.query];
				if (p.count !== undefined) args.push("--count", String(p.count));
				if (p.ori_question) args.push("--ori-question", p.ori_question);
				if (p.need_content) args.push("--need-content");
				const result = await invokeDeepSeekWebCli(args);
				if (!result.ok) {
					return toolError(result.error ?? "DeepSeek web search failed");
				}
				const text = result.text ?? "(no search results)";
				return {
					content: [{ type: "text" as const, text }],
					...(text === "(no search results)" ? { useless: true as const } : {}),
				};
			} catch (err) {
				return toolError(
					`UC DeepSeek web search error: ${err instanceof Error ? err.message : String(err)}`,
				);
			}
		},
	});

	const fetchSchema = pi.zod.object({
		url: pi.zod.string().describe("Public https URL to fetch (no username/password)"),
	});

	pi.registerTool({
		name: "uc_deepseek_web_fetch",
		label: "DeepSeek Web Fetch",
		description:
			"Fetch one public https URL via DeepSeek official Fetch API (UC Python → POST /v1/tools/fetch). " +
			"UC rejects non-https, userinfo, and private/metadata targets. " +
			"Account QPS 5; $0.75 / 1k successful calls (agent-embedded grace ends 2026-11-01). " +
			"Requires DEEPSEEK_API_KEY.",
		parameters: fetchSchema as never,
		async execute(_id, params: unknown, _signal, _onUpdate, _ctx) {
			const p = params as { url: string };
			try {
				if (!isDeepSeekWebEnabled()) {
					return toolError(
						"DEEPSEEK_API_KEY is not set. Set it to enable DeepSeek Search/Fetch.",
					);
				}
				const result = await invokeDeepSeekWebCli(["fetch", "--url", p.url]);
				if (!result.ok) {
					return toolError(result.error ?? "DeepSeek web fetch failed");
				}
				return { content: [{ type: "text" as const, text: result.text ?? "(empty)" }] };
			} catch (err) {
				return toolError(
					`UC DeepSeek web fetch error: ${err instanceof Error ? err.message : String(err)}`,
				);
			}
		},
	});
}
