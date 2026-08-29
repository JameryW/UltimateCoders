/**
 * WatchTask recovery regressions.
 *
 * A bounded reconnect sequence must not leave the live event stream stranded
 * after a long gateway outage. The recovery timer either reopens a connected
 * bridge directly or lets the bridge health RPC restore connectivity first.
 *
 * Run: bun test src/orchestrator/watch-task-recovery.test.ts
 */

import { describe, expect, it, mock } from "bun:test";

mock.module("@oh-my-pi/pi-coding-agent", () => ({
	runSubprocess: async () => ({ stdout: "", stderr: "", code: 0 }),
}));

import { UCOrchestrator } from "./orchestrator";
import type { GrpcBridge } from "./grpc-bridge";

type WatchTaskInternals = {
	startWatchTaskStream: () => void;
	scheduleWatchTaskRecovery: (generation: number) => void;
	watchTaskStreamGeneration: number;
	watchTaskReconnectAttempt: number;
};

function makeOrchestrator(connected: boolean) {
	let healthCalls = 0;
	const streams: Array<{ onError: () => void }> = [];
	const pi = {
		pi: { settings: { workspaceRoot: process.cwd() } },
		logger: { warn: () => {}, info: () => {} },
	};
	const bridge = {
		isConnected: () => connected,
		health: async () => {
			healthCalls++;
			connected = true;
			return { status: "ok", version: "test" };
		},
		setOnConnectionChange: () => {},
		setOnReconnectAttempt: () => {},
		startWatchTask: (_onEvent: unknown, onError: () => void) => {
			streams.push({ onError });
			return new AbortController();
		},
		close: () => {},
	} as unknown as GrpcBridge;
	const orchestrator = new UCOrchestrator(pi as never, undefined, bridge);
	return {
		orchestrator,
		internals: orchestrator as unknown as WatchTaskInternals,
		streams,
		getHealthCalls: () => healthCalls,
	};
}

async function withFakeTimeout(run: (fire: () => void, delay: () => number | undefined) => void | Promise<void>): Promise<void> {
	const realSetTimeout = globalThis.setTimeout;
	let callback: (() => void) | undefined;
	let requestedDelay: number | undefined;
	globalThis.setTimeout = ((handler: TimerHandler, timeout?: number) => {
		requestedDelay = timeout;
		callback = typeof handler === "function" ? () => handler() : undefined;
		return { unref: () => {} } as unknown as ReturnType<typeof setTimeout>;
	}) as unknown as typeof globalThis.setTimeout;
	try {
		await run(
			() => callback?.(),
			() => requestedDelay,
		);
	} finally {
		globalThis.setTimeout = realSetTimeout;
	}
}

describe("WatchTask stream recovery", () => {
	it("reopens a connected stream after the bounded backoff is exhausted", async () => {
		const { orchestrator, internals, streams } = makeOrchestrator(true);
		internals.startWatchTaskStream();
		expect(streams).toHaveLength(1);

		await withFakeTimeout(async (fire, delay) => {
			// The production backoff helper returns false immediately when the
			// current attempt has reached maxAttempts, without sleeping 91s.
			internals.watchTaskReconnectAttempt = 8;
			streams[0].onError();
			await Promise.resolve();

			internals.scheduleWatchTaskRecovery(internals.watchTaskStreamGeneration);
			// A second scheduling attempt must not create a duplicate timer.
			internals.scheduleWatchTaskRecovery(internals.watchTaskStreamGeneration);
			expect(delay()).toBe(30_000);

			fire();
			expect(streams).toHaveLength(2);
		});

		await orchestrator.destroy();
	});

	it("probes a disconnected bridge before reopening the stream", async () => {
		const state = makeOrchestrator(false);
		state.internals.startWatchTaskStream();

		await withFakeTimeout((fire) => {
			state.internals.scheduleWatchTaskRecovery(state.internals.watchTaskStreamGeneration);
			fire();
		});

		await Promise.resolve();
		expect(state.getHealthCalls()).toBe(1);
		expect(state.streams).toHaveLength(2);
		await state.orchestrator.destroy();
	});
});
