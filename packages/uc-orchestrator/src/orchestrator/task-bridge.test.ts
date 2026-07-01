/**
 * task-bridge spawn-gating tests.
 *
 * Verifies that `UC_NO_SPAWN` env hard-blocks `uc_task submit` (Path B)
 * without calling bridge.submitTask or orchestrator.submitTask, and that
 * when unset, submission proceeds normally.
 *
 * Run: bun test src/orchestrator/task-bridge.test.ts
 */

import { describe, expect, it, afterEach, beforeEach, mock } from "bun:test";
import { isSpawnDisabled } from "./task-bridge";

// --- isSpawnDisabled unit tests -------------------------------------------

describe("isSpawnDisabled", () => {
	const orig = process.env.UC_NO_SPAWN;

	afterEach(() => {
		if (orig === undefined) delete process.env.UC_NO_SPAWN;
		else process.env.UC_NO_SPAWN = orig;
	});

	it("returns false when UC_NO_SPAWN is unset", () => {
		delete process.env.UC_NO_SPAWN;
		expect(isSpawnDisabled()).toBe(false);
	});

	it("returns true when UC_NO_SPAWN=1", () => {
		process.env.UC_NO_SPAWN = "1";
		expect(isSpawnDisabled()).toBe(true);
	});

	it("returns true when UC_NO_SPAWN is any non-empty string", () => {
		process.env.UC_NO_SPAWN = "true";
		expect(isSpawnDisabled()).toBe(true);
	});
});

// --- uc_task submit gate (tool-level integration) -------------------------

describe("uc_task submit spawn-gate", () => {
	const orig = process.env.UC_NO_SPAWN;
	let captured: { name: string; parameters: unknown; execute: (...args: unknown[]) => Promise<unknown> } | null = null;

	function makeMockPi() {
		captured = null;
		// Minimal zod mock: every method returns a self-similar chainable object.
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
		const zod = {
			object: () => chainable(),
			enum: () => chainable(),
			string: () => chainable(),
		};
		return {
			zod,
			logger: { info: () => {}, warn: () => {}, error: () => {} },
			registerTool: mock((def: { name: string; parameters: unknown; execute: (...args: unknown[]) => Promise<unknown> }) => {
				captured = def;
			}),
			registerCommand: mock(() => {}),
		} as unknown as import("@oh-my-pi/pi-coding-agent").ExtensionAPI;
	}

	function makeMockBridge() {
		return {
			submitTask: mock(() => Promise.resolve({ ok: true, task: { taskId: "t-1", status: "planning", subtasks: [] } })),
		} as unknown as import("./grpc-bridge").GrpcBridge;
	}

	function makeMockOrchestrator() {
		return {
			submitTask: mock(() => Promise.resolve("t-1")),
		} as unknown as import("./orchestrator").UCOrchestrator;
	}

	afterEach(() => {
		if (orig === undefined) delete process.env.UC_NO_SPAWN;
		else process.env.UC_NO_SPAWN = orig;
	});

	it("blocks submit when UC_NO_SPAWN=1 and does NOT call bridge/orchestrator", async () => {
		process.env.UC_NO_SPAWN = "1";
		const { registerTaskTools } = await import("./task-bridge");
		const pi = makeMockPi();
		const bridge = makeMockBridge();
		const orch = makeMockOrchestrator();
		registerTaskTools(pi, bridge, orch);
		expect(captured).not.toBeNull();
		const result = await captured!.execute("id", { action: "submit", description: "do thing" }, undefined, undefined, undefined) as { content: { text: string }[]; isError?: boolean };
		expect(result.isError).toBe(true);
		expect(result.content[0].text).toContain("UC_NO_SPAWN");
		expect(bridge.submitTask).not.toHaveBeenCalled();
		expect((orch as unknown as { submitTask: { mock: { calls: unknown[] } } }).submitTask).not.toHaveBeenCalled();
	});

	it("proceeds normally when UC_NO_SPAWN is unset", async () => {
		delete process.env.UC_NO_SPAWN;
		const { registerTaskTools } = await import("./task-bridge");
		const pi = makeMockPi();
		const bridge = makeMockBridge();
		registerTaskTools(pi, bridge, makeMockOrchestrator());
		expect(captured).not.toBeNull();
		const result = await captured!.execute("id", { action: "submit", description: "do thing" }, undefined, undefined, undefined) as { content: { text: string }[]; isError?: boolean };
		expect(result.isError).toBeUndefined();
		expect(bridge.submitTask).toHaveBeenCalled();
		expect(result.content[0].text).toContain("Task submitted");
	});
});
