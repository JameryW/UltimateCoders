/**
 * sleepCancellable self-check — retry backoff that respects abort.
 *
 * Regression: executeSubtaskWithRetry's backoff was a bare setTimeout that
 * ignored the task's AbortSignal, so cancelling a task during retry backoff
 * (or shutting down) waited out the full delay before stopping. The cancellable
 * sleep resolves early on abort so cancel is responsive.
 *
 * Run: bun test src/orchestrator/sleep-cancellable.test.ts
 */

import { describe, expect, it } from "bun:test";
import { sleepCancellable } from "./orchestrator";

describe("sleepCancellable", () => {
	it("returns false after the delay elapses (no abort)", async () => {
		const start = Date.now();
		const aborted = await sleepCancellable(30);
		const elapsed = Date.now() - start;
		expect(aborted).toBe(false);
		expect(elapsed).toBeGreaterThanOrEqual(25);
	});

	it("returns true immediately when the signal is already aborted", async () => {
		const ctrl = new AbortController();
		ctrl.abort();
		const aborted = await sleepCancellable(1000, ctrl.signal);
		expect(aborted).toBe(true);
	});

	it("resolves early when the signal aborts mid-sleep", async () => {
		const ctrl = new AbortController();
		const start = Date.now();
		// Abort after 20ms; sleep is 1000ms — must resolve well before 1000ms.
		setTimeout(() => ctrl.abort(), 20);
		const aborted = await sleepCancellable(1000, ctrl.signal);
		const elapsed = Date.now() - start;
		expect(aborted).toBe(true);
		expect(elapsed).toBeLessThan(500);
	});

	it("does not throw when called without a signal", async () => {
		await expect(sleepCancellable(10)).resolves.toBe(false);
	});
});
