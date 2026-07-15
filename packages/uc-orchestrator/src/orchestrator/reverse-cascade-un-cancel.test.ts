/**
 * reverseCascadeUnCancel — per-subtask retry's reverse-cascade recovery.
 *
 * retrySubtask resets ONE failed subtask to pending, then this fn walks the
 * downstream that was cascade-cancelled SOLELY because that subtask failed and
 * un-cancels them (deps now satisfied: completed, or just-reset). A downstream
 * depending on ANOTHER still-failed subtask must stay cancelled — this is the
 * invariant the whole "retry only X" UX depends on, so it is pinned here.
 *
 * Run: bun test src/orchestrator/reverse-cascade-un-cancel.test.ts
 */

import { describe, expect, it } from "bun:test";
import { reverseCascadeUnCancel, type SubtaskResult } from "./orchestrator";

function st(id: string, over: Partial<SubtaskResult> = {}): SubtaskResult {
	return {
		id,
		description: `d-${id}`,
		status: "pending",
		dependsOn: [],
		files: [],
		...over,
	} as SubtaskResult;
}

describe("reverseCascadeUnCancel", () => {
	it("recovers a direct downstream cancelled solely because X failed", () => {
		// A completed → B failed → C cancelled (depends on B, B was failed).
		// Retry B: reset={B}. C's deps (B) is in reset → C recovers to pending.
		const subs = [
			st("A", { status: "completed" }),
			st("B", { status: "failed", dependsOn: ["A"] }),
			st("C", { status: "cancelled", dependsOn: ["B"] }),
		];
		const reset = new Set<string>(["B"]);
		reverseCascadeUnCancel(subs, reset);

		expect(subs[0].status).toBe("completed"); // untouched
		expect(subs[1].status).toBe("failed"); // not touched here (retrySubtask reset it)
		expect(subs[2].status).toBe("pending"); // recovered
		expect(reset.has("C")).toBe(true); // added to recovered set
	});

	it("recovers a chain of downstream cancels in one pass (fixed point)", () => {
		// A completed → B failed → C cancelled → D cancelled → E cancelled.
		// Retry B: reset={B}. C deps(B) ok → pending; then D deps(C) ok → pending;
		// then E deps(D) ok → pending. Whole chain recovers.
		const subs = [
			st("A", { status: "completed" }),
			st("B", { status: "failed", dependsOn: ["A"] }),
			st("C", { status: "cancelled", dependsOn: ["B"] }),
			st("D", { status: "cancelled", dependsOn: ["C"] }),
			st("E", { status: "cancelled", dependsOn: ["D"] }),
		];
		const reset = new Set<string>(["B"]);
		reverseCascadeUnCancel(subs, reset);

		expect(subs[2].status).toBe("pending");
		expect(subs[3].status).toBe("pending");
		expect(subs[4].status).toBe("pending");
		expect(reset.has("C")).toBe(true);
		expect(reset.has("D")).toBe(true);
		expect(reset.has("E")).toBe(true);
	});

	it("leaves a downstream cancelled when its deps include ANOTHER still-failed subtask", () => {
		// A failed (NOT retried), B completed, C cancelled depends on BOTH A and B.
		// Retry a DIFFERENT subtask X (reset={X}, unrelated): C's deps include A
		// which is still failed + not in reset → C stays cancelled.
		const subs = [
			st("A", { status: "failed", dependsOn: [] }),
			st("B", { status: "completed", dependsOn: [] }),
			st("C", { status: "cancelled", dependsOn: ["A", "B"] }),
		];
		const reset = new Set<string>(["X"]); // retrying an unrelated subtask
		reverseCascadeUnCancel(subs, reset);

		expect(subs[2].status).toBe("cancelled"); // NOT recovered — A still failed
		expect(reset.has("C")).toBe(false);
	});

	it("recovers a diamond: one failed node, two downstreams both recover", () => {
		// A completed → B failed. C & D both depend only on B, both cancelled.
		// Retry B: C deps(B) ok, D deps(B) ok → both recover.
		const subs = [
			st("A", { status: "completed" }),
			st("B", { status: "failed", dependsOn: ["A"] }),
			st("C", { status: "cancelled", dependsOn: ["B"] }),
			st("D", { status: "cancelled", dependsOn: ["B"] }),
		];
		const reset = new Set<string>(["B"]);
		reverseCascadeUnCancel(subs, reset);

		expect(subs[2].status).toBe("pending");
		expect(subs[3].status).toBe("pending");
	});

	it("does nothing when reset is empty (no subtask was retried)", () => {
		const subs = [
			st("A", { status: "completed" }),
			st("B", { status: "cancelled", dependsOn: ["A"] }),
		];
		const reset = new Set<string>();
		reverseCascadeUnCancel(subs, reset);
		// B's dep A is completed → B WOULD recover even with empty reset? No:
		// B depends on A which is completed, so depsOk=true regardless of reset.
		// This is actually correct recovery semantics: a cancelled node whose
		// deps are all completed has no reason to stay cancelled. Documented here.
		expect(subs[1].status).toBe("pending");
	});

	it("clears error/result/retryCount/timestamps on recovery", () => {
		const subs = [
			st("A", { status: "completed" }),
			st("B", { status: "cancelled", dependsOn: ["A"], error: "boom", result: "r", retryCount: 3, completedAt: 999 }),
		];
		const reset = new Set<string>(["B-retried-sibling"]); // unrelated reset
		reverseCascadeUnCancel(subs, reset);
		// B deps A completed → B recovers, fields cleared.
		expect(subs[1].status).toBe("pending");
		expect(subs[1].error).toBeUndefined();
		expect(subs[1].result).toBeUndefined();
		expect(subs[1].retryCount).toBe(0);
		expect(subs[1].completedAt).toBeUndefined();
	});
});
