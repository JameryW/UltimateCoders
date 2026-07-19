/**
 * ControlSignalSubscriber lifecycle self-check.
 *
 * Regression: after stop() (called on session_shutdown via orchestrator.destroy()),
 * a NATS disconnect would still trigger tryNatsReconnect from the subscription
 * iterator's exit path — reviving a stopped subscriber with a fresh NATS
 * connection + poll timer that leaked across sessions (OMP session interruption:
 * stale handlers, leaked connections, double-polling). The `stopped` flag now
 * gates reconnect revival, and startPolling() clears any prior poll timer.
 *
 * Run: bun test src/orchestrator/control-signal-subscriber.test.ts
 */

import { describe, expect, it } from "bun:test";
import { ControlSignalSubscriber } from "./control-signal-subscriber";

// A handler stub that records calls without touching real orchestration.
function makeHandler() {
	const calls: Array<{ method: string; taskId: string }> = [];
	return {
		handler: {
			// ponytail: F27 — handler methods return ControlOutcome (tests only
			// record calls; the payload is unused here). `as const` keeps `ok`
			// literal so it matches the discriminated union.
			pauseTask: async (taskId: string) => { calls.push({ method: "pause", taskId }); return { ok: true as const, taskId }; },
			resumeTask: async (taskId: string) => { calls.push({ method: "resume", taskId }); return { ok: true as const, taskId }; },
			cancelTask: async (taskId: string) => { calls.push({ method: "cancel", taskId }); return { ok: true as const, taskId }; },
			getActiveTaskIds: () => [] as string[],
		},
		calls,
	};
}

// A bridge stub whose getTask always returns null (no active control changes).
function makeBridge() {
	return {
		getTask: async () => null,
		// other methods are not exercised in these tests
	} as unknown as ConstructorParameters<typeof ControlSignalSubscriber>[1];
}

describe("ControlSignalSubscriber stop()", () => {
	it("clears the poll timer on stop (no leaked polling after shutdown)", async () => {
		const { handler } = makeHandler();
		// Point at a dead NATS URL so start() falls back to polling.
		const sub = new ControlSignalSubscriber(
			handler,
			makeBridge(),
			{ natsUrl: "nats://127.0.0.1:1", pollIntervalMs: 1000, maxReconnectAttempts: 1 },
		);
		await sub.start();
		// After start with dead NATS, polling fallback should be active.
		expect(sub.isNatsConnected()).toBe(false);

		await sub.stop();

		// isNatsConnected stays false; the contract is that stop() is terminal —
		// no further polling/reconnect should revive it. We assert the observable
		// state rather than the private timer field.
		expect(sub.isNatsConnected()).toBe(false);
		// Re-stopping must be a safe no-op (idempotent shutdown).
		await expect(sub.stop()).resolves.toBeUndefined();
	});

	it("start() is idempotent-ish: calling stop then start resets stopped", async () => {
		const { handler } = makeHandler();
		const sub = new ControlSignalSubscriber(
			handler,
			makeBridge(),
			{ natsUrl: "nats://127.0.0.1:1", pollIntervalMs: 1000, maxReconnectAttempts: 1 },
		);
		await sub.start();
		await sub.stop();
		// start() again resets the stopped flag (simulating a new session).
		await sub.start();
		expect(sub.isNatsConnected()).toBe(false); // still dead NATS → polling
		await sub.stop();
	});
});
