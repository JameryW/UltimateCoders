/**
 * backoff.ts — exponential backoff helper self-check.
 *
 * Run: bun test src/orchestrator/backoff.test.ts
 */

import { describe, expect, it } from "bun:test";
import { backoffDelay, sleepBackoff } from "./backoff";

describe("backoffDelay", () => {
	it("doubles the delay each attempt up to the cap", () => {
		expect(backoffDelay(0, { initialMs: 100, maxMs: 1000, maxAttempts: 5 })).toBe(100);
		expect(backoffDelay(1, { initialMs: 100, maxMs: 1000, maxAttempts: 5 })).toBe(200);
		expect(backoffDelay(2, { initialMs: 100, maxMs: 1000, maxAttempts: 5 })).toBe(400);
		expect(backoffDelay(3, { initialMs: 100, maxMs: 1000, maxAttempts: 5 })).toBe(800);
		// capped at maxMs
		expect(backoffDelay(4, { initialMs: 100, maxMs: 1000, maxAttempts: 5 })).toBe(1000);
	});

	it("returns null when attempt >= maxAttempts", () => {
		expect(backoffDelay(5, { maxAttempts: 5 })).toBeNull();
		expect(backoffDelay(0, { maxAttempts: 0 })).toBeNull();
	});

	it("uses sensible production defaults", () => {
		expect(backoffDelay(0)).toBe(500);
		expect(backoffDelay(1)).toBe(1000);
		// default maxAttempts = 5 → attempt 5+ returns null
		expect(backoffDelay(4)).toBe(8000);
		expect(backoffDelay(5)).toBeNull();
		// the cap is reachable with a low maxAttempts + high attempt, but
		// defaults cut off at 5 attempts before reaching the 30s cap.
		expect(backoffDelay(10, { maxAttempts: 11 })).toBe(30_000);
	});

	it("applies jitter within bounds", () => {
		for (let i = 0; i < 50; i++) {
			const d = backoffDelay(2, { initialMs: 1000, maxMs: 10_000, maxAttempts: 5, jitter: 0.25 });
			if (d === null) continue;
			// 4000 ± 25% → [3000, 5000]
			expect(d).toBeGreaterThanOrEqual(3000);
			expect(d).toBeLessThanOrEqual(5000);
		}
	});
});

describe("sleepBackoff", () => {
	it("returns false and does not sleep when attempt >= maxAttempts", async () => {
		const start = Date.now();
		const result = await sleepBackoff(5, { maxAttempts: 5 });
		const elapsed = Date.now() - start;
		expect(result).toBe(false);
		expect(elapsed).toBeLessThan(50);
	});

	it("sleeps for roughly the computed delay", async () => {
		const start = Date.now();
		const result = await sleepBackoff(0, { initialMs: 50, maxMs: 1000, maxAttempts: 5 });
		const elapsed = Date.now() - start;
		expect(result).toBe(true);
		// Allow generous slack for timer scheduling.
		expect(elapsed).toBeGreaterThanOrEqual(40);
		expect(elapsed).toBeLessThan(500);
	});

	// ponytail: F10 — onAttempt reports the exact delay about to be slept, so a
	// UI countdown matches reality even under jitter (no second computation).
	it("calls onAttempt with the attempt and exact delay before sleeping", async () => {
		const calls: [number, number][] = [];
		const result = await sleepBackoff(2, {
			initialMs: 10, maxMs: 1000, maxAttempts: 5,
			onAttempt: (a, d) => calls.push([a, d]),
		});
		expect(result).toBe(true);
		expect(calls).toEqual([[2, 40]]); // 10 * 2^2
	});

	it("does not call onAttempt when backoff is exhausted", async () => {
		const calls: [number, number][] = [];
		const result = await sleepBackoff(5, {
			maxAttempts: 5,
			onAttempt: (a, d) => calls.push([a, d]),
		});
		expect(result).toBe(false);
		expect(calls).toEqual([]);
	});
});
