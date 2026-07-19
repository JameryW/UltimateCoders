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

// ponytail: F30 — subtask-level cancel must route through the local
// orchestrator (real subtask cancel + cascade). gRPC CancelTaskRequest has
// no subtask field — the old code sent it to the bridge (server cancelled
// the WHOLE task) and reported the subtask as cancelled.
describe("uc_task cancel routing", () => {
	let captured: { name: string; parameters: unknown; execute: (...args: unknown[]) => Promise<unknown> } | null = null;

	function makeMockPi() {
		captured = null;
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
			zod: { object: () => chainable(), enum: () => chainable(), string: () => chainable() },
			logger: { info: () => {}, warn: () => {}, error: () => {} },
			registerTool: mock((def: { name: string; parameters: unknown; execute: (...args: unknown[]) => Promise<unknown> }) => {
				captured = def;
			}),
			registerCommand: mock(() => {}),
		} as unknown as import("@oh-my-pi/pi-coding-agent").ExtensionAPI;
	}

	async function runCancel(params: Record<string, unknown>, opts: {
		bridgeCancel?: ReturnType<typeof mock>;
		orchCancel?: ReturnType<typeof mock>;
	}) {
		const { registerTaskTools } = await import("./task-bridge");
		const bridgeCancel = opts.bridgeCancel ?? mock(() => Promise.resolve(true));
		const orchCancel = opts.orchCancel ?? mock(() => Promise.resolve({ ok: true, taskId: "t-1" }));
		// ponytail: isConnected true — these cases cover the server-up paths
		// (F40's down-server branch is tested in "uc_task verbs with gRPC down").
		const bridge = { cancelTask: bridgeCancel, isConnected: () => true } as unknown as import("./grpc-bridge").GrpcBridge;
		const orch = { cancelTask: orchCancel } as unknown as import("./orchestrator").UCOrchestrator;
		registerTaskTools(makeMockPi(), bridge, orch);
		expect(captured).not.toBeNull();
		const result = await captured!.execute("id", params, undefined, undefined, undefined) as { content: { text: string }[] };
		return { result, bridgeCancel, orchCancel };
	}

	it("routes subtask cancel through the orchestrator, not the bridge", async () => {
		const { result, bridgeCancel, orchCancel } = await runCancel(
			{ action: "cancel", task_id: "t-1", subtask_id: "st-2" }, {});
		expect(orchCancel).toHaveBeenCalledWith("t-1", "st-2");
		expect(bridgeCancel).not.toHaveBeenCalled();
		expect(result.content[0].text).toContain("Cancelled subtask st-2");
		expect(result.content[0].text).toContain("cascade");
	});

	it("surfaces the discriminated failure reason from the orchestrator", async () => {
		const orchCancel = mock(() => Promise.resolve({ ok: false, reason: "subtask_not_found", candidates: ["st-1", "st-3"] }));
		const { result } = await runCancel(
			{ action: "cancel", task_id: "t-1", subtask_id: "st-99" }, { orchCancel });
		expect(result.content[0].text).toContain("subtask_not_found");
		expect(result.content[0].text).toContain("st-1");
	});

	it("whole-task cancel without subtask_id stays on the bridge", async () => {
		const { result, bridgeCancel, orchCancel } = await runCancel(
			{ action: "cancel", task_id: "t-1" }, {});
		expect(bridgeCancel).toHaveBeenCalled();
		expect(orchCancel).not.toHaveBeenCalled();
		expect(result.content[0].text).toContain("Cancelled task t-1");
	});
});

// ponytail: F40 — a down gRPC server collapsed to bridge fallbacks (false/
// null/[]), so the tools reported "not found"/"not in progress"/"(no tasks)"
// and LLMs concluded tasks didn't exist. Verbs now check isConnected() and
// fall back to the local orchestrator with an explicit "unavailable" wording.
describe("uc_task verbs with gRPC down", () => {
	let captured: { name: string; parameters: unknown; execute: (...args: unknown[]) => Promise<unknown> } | null = null;

	function makeMockPi() {
		captured = null;
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
			zod: { object: () => chainable(), enum: () => chainable(), string: () => chainable() },
			logger: { info: () => {}, warn: () => {}, error: () => {} },
			registerTool: mock((def: { name: string; parameters: unknown; execute: (...args: unknown[]) => Promise<unknown> }) => {
				captured = def;
			}),
			registerCommand: mock(() => {}),
		} as unknown as import("@oh-my-pi/pi-coding-agent").ExtensionAPI;
	}

	async function run(params: Record<string, unknown>, opts: {
		connected: boolean;
		bridgePause?: ReturnType<typeof mock>;
		orchPause?: ReturnType<typeof mock>;
		orchList?: () => unknown[];
		withOrchestrator?: boolean;
	}) {
		const { registerTaskTools } = await import("./task-bridge");
		const bridgePause = opts.bridgePause ?? mock(() => Promise.resolve(true));
		const bridge = {
			isConnected: () => opts.connected,
			pauseTask: bridgePause,
		} as unknown as import("./grpc-bridge").GrpcBridge;
		const orch = opts.withOrchestrator === false ? undefined : ({
			pauseTask: opts.orchPause ?? mock(() => Promise.resolve({ ok: true as const, taskId: "t-1" })),
			getAllTaskStates: opts.orchList ?? (() => []),
		} as unknown as import("./orchestrator").UCOrchestrator);
		registerTaskTools(makeMockPi(), bridge, orch);
		expect(captured).not.toBeNull();
		const result = await captured!.execute("id", params, undefined, undefined, undefined) as { content: { text: string }[]; isError?: boolean };
		return { result, bridgePause };
	}

	it("pause with server down routes to the local orchestrator", async () => {
		const { result, bridgePause } = await run(
			{ action: "pause", task_id: "t-1" }, { connected: false });
		expect(bridgePause).not.toHaveBeenCalled();
		expect(result.content[0].text).toContain("local");
		expect(result.content[0].text).toContain("unavailable");
	});

	it("pause with server down and no local match says unavailable, not 'not in progress'", async () => {
		const orchPause = mock(() => Promise.resolve({ ok: false, reason: "not_found", candidates: ["t-9"] }));
		const { result } = await run(
			{ action: "pause", task_id: "zz" }, { connected: false, orchPause });
		expect(result.content[0].text).toContain("unavailable");
		expect(result.content[0].text).not.toContain("not in progress");
		expect(result.content[0].text).toContain("t-9");
	});

	it("pause with no orchestrator reports plain unavailability", async () => {
		const { result } = await run(
			{ action: "pause", task_id: "t-1" }, { connected: false, withOrchestrator: false });
		expect(result.content[0].text).toBe("Pause failed: gRPC server unavailable");
	});

	it("status list with server down shows the local view with a banner", async () => {
		const { result } = await run(
			{ action: "status" },
			{
				connected: false,
				orchList: () => [{ id: "t-1", status: "in_progress", description: "doing things", subtasks: [] }],
			});
		expect(result.content[0].text).toContain("[in_progress] t-1");
		expect(result.content[0].text).toContain("local view");
	});

	it("pause with server up stays on the bridge (regression)", async () => {
		const orchPause = mock(() => Promise.resolve({ ok: true as const, taskId: "t-1" }));
		const { result, bridgePause } = await run(
			{ action: "pause", task_id: "t-1" }, { connected: true, orchPause });
		expect(bridgePause).toHaveBeenCalled();
		expect(orchPause).not.toHaveBeenCalled();
		expect(result.content[0].text).toBe("Paused task t-1");
	});
});
