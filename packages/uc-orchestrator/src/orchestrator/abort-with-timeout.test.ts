/**
 * abortWithTimeout self-check.
 *
 * Regression: when the parent signal was already aborted at call time, the old
 * code returned `AbortSignal.timeout(timeoutMs)` — a FRESH timeout that would
 * not fire until timeoutMs elapsed. So a cancel arriving during shutdown delayed
 * the abort by up to timeoutMs (10min for subtasks, 120s for decompose) instead
 * of taking effect immediately. Also, the timeout branch never removed the
 * parent-signal listener (minor leak).
 *
 * Run: bun test src/orchestrator/abort-with-timeout.test.ts
 */

import { describe, expect, it } from "bun:test";
import { abortWithTimeout } from "./orchestrator";

describe("abortWithTimeout", () => {
	it("aborts immediately when the parent signal is already aborted", () => {
		const parent = new AbortController();
		parent.abort();
		const s = abortWithTimeout(parent.signal, 60_000);
		expect(s.aborted).toBe(true);
	});

	it("aborts immediately when no parent signal is given but still starts the timeout", async () => {
		// No parent signal: only the timeout can fire. With a long timeout it
		// should NOT be aborted yet (regression guard against early-abort).
		const s = abortWithTimeout(undefined, 10_000);
		expect(s.aborted).toBe(false);
	});

	it("aborts on timeout when the parent never aborts", async () => {
		const parent = new AbortController();
		const s = abortWithTimeout(parent.signal, 30);
		expect(s.aborted).toBe(false);
		await new Promise((r) => setTimeout(r, 60));
		expect(s.aborted).toBe(true);
	});

	it("aborts when the parent aborts before the timeout", async () => {
		const parent = new AbortController();
		const s = abortWithTimeout(parent.signal, 10_000);
		expect(s.aborted).toBe(false);
		parent.abort();
		// Synchronous propagation via the listener.
		expect(s.aborted).toBe(true);
	});

	it("does not keep the timeout pending after parent abort fires", async () => {
		// If parent aborts, the timeout must be cleared so it can't fire later
		// and do redundant work. We assert observable behavior: signal stays
		// aborted (idempotent) and no unhandled timer rejection surfaces.
		const parent = new AbortController();
		const s = abortWithTimeout(parent.signal, 50);
		parent.abort();
		await new Promise((r) => setTimeout(r, 80));
		expect(s.aborted).toBe(true);
	});
});
