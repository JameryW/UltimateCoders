/**
 * DeepSeek web bridge registration / enablement tests.
 *
 * Run: bun test src/orchestrator/deepseek-web-bridge.test.ts
 */

import { afterEach, describe, expect, it, mock } from "bun:test";
import {
	isDeepSeekWebEnabled,
	registerDeepSeekWebTools,
} from "./deepseek-web-bridge";

describe("isDeepSeekWebEnabled", () => {
	const orig = process.env.DEEPSEEK_API_KEY;

	afterEach(() => {
		if (orig === undefined) delete process.env.DEEPSEEK_API_KEY;
		else process.env.DEEPSEEK_API_KEY = orig;
	});

	it("returns false when key unset", () => {
		delete process.env.DEEPSEEK_API_KEY;
		expect(isDeepSeekWebEnabled()).toBe(false);
	});

	it("returns false when key blank", () => {
		process.env.DEEPSEEK_API_KEY = "   ";
		expect(isDeepSeekWebEnabled()).toBe(false);
	});

	it("returns true when key present", () => {
		process.env.DEEPSEEK_API_KEY = "sk-test";
		expect(isDeepSeekWebEnabled()).toBe(true);
	});
});

describe("registerDeepSeekWebTools", () => {
	const orig = process.env.DEEPSEEK_API_KEY;
	const tools: Array<{ name: string; execute: (...args: unknown[]) => Promise<unknown> }> = [];

	function makeMockPi() {
		tools.length = 0;
		const chainable = (): unknown => {
			const fn = () => chainable();
			return new Proxy(fn, {
				get: (_t, prop) => {
					if (typeof prop === "string") return () => chainable();
					return undefined;
				},
				apply: () => chainable(),
			});
		};
		return {
			zod: {
				object: () => chainable(),
				string: () => chainable(),
				number: () => chainable(),
				boolean: () => chainable(),
			},
			logger: { info: () => {}, warn: () => {}, error: () => {} },
			registerTool: mock((def: { name: string; execute: (...args: unknown[]) => Promise<unknown> }) => {
				tools.push(def);
			}),
			registerCommand: mock(() => {}),
		} as unknown as import("@oh-my-pi/pi-coding-agent").ExtensionAPI;
	}

	afterEach(() => {
		if (orig === undefined) delete process.env.DEEPSEEK_API_KEY;
		else process.env.DEEPSEEK_API_KEY = orig;
	});

	it("registers no tools when key missing", () => {
		delete process.env.DEEPSEEK_API_KEY;
		registerDeepSeekWebTools(makeMockPi());
		expect(tools).toHaveLength(0);
	});

	it("registers search and fetch tools when key present", () => {
		process.env.DEEPSEEK_API_KEY = "sk-test";
		registerDeepSeekWebTools(makeMockPi());
		expect(tools.map((t) => t.name).sort()).toEqual([
			"uc_deepseek_web_fetch",
			"uc_deepseek_web_search",
		]);
	});

	it("fail-closed when key cleared after registration", async () => {
		process.env.DEEPSEEK_API_KEY = "sk-test";
		registerDeepSeekWebTools(makeMockPi());
		const search = tools.find((t) => t.name === "uc_deepseek_web_search");
		expect(search).toBeDefined();
		delete process.env.DEEPSEEK_API_KEY;
		const result = (await search!.execute(
			"id",
			{ query: "python" },
			undefined,
			undefined,
			undefined,
		)) as { isError?: boolean; content: { text: string }[] };
		expect(result.isError).toBe(true);
		expect(result.content[0].text).toContain("DEEPSEEK_API_KEY");
	});
});
