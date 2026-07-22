/**
 * parseReviewOutput self-check — fail-CLOSED on the supervisor review gate.
 *
 * Regression: `parsed.approved ?? true` + `catch { approved: true }` were
 * fail-OPEN — a missing/non-boolean `approved`, or an unparseable review
 * output, silently APPROVED a possibly-defective subtask. Only an explicit
 * `approved: true` may approve; everything else rejects.
 *
 * Run: bun test src/orchestrator/parse-review.test.ts
 */

import { describe, expect, it } from "bun:test";
import { parseReviewOutput } from "./orchestrator";

describe("parseReviewOutput", () => {
	it("approves on explicit approved: true", () => {
		const r = parseReviewOutput(JSON.stringify({ approved: true, issues: [], suggestions: ["ship it"] }));
		expect(r.approved).toBe(true);
		expect(r.suggestions).toEqual(["ship it"]);
	});

	it("rejects on explicit approved: false", () => {
		const r = parseReviewOutput(JSON.stringify({ approved: false, issues: ["bug X"], suggestions: [] }));
		expect(r.approved).toBe(false);
		expect(r.issues).toEqual(["bug X"]);
	});

	it("REJECTS (was approve) when approved key is missing", () => {
		const r = parseReviewOutput(JSON.stringify({ issues: [], suggestions: [] }));
		expect(r.approved).toBe(false);
	});

	it("rejects when approved is a truthy non-boolean (string 'true')", () => {
		// strict === true: string must not count as approval
		const r = parseReviewOutput(JSON.stringify({ approved: "true" }));
		expect(r.approved).toBe(false);
	});

	it("rejects when approved is 1 (truthy number)", () => {
		const r = parseReviewOutput(JSON.stringify({ approved: 1 }));
		expect(r.approved).toBe(false);
	});

	it("REJECTS (was approve) and records an issue on unparseable output", () => {
		const r = parseReviewOutput("not json {{{");
		expect(r.approved).toBe(false);
		expect(r.issues.length).toBe(1);
		expect(r.issues[0]).toMatch(/could not be parsed/i);
	});

	it("invokes onParseError on unparseable output", () => {
		let called = false;
		parseReviewOutput("not json", () => { called = true; });
		expect(called).toBe(true);
	});

	it("does NOT invoke onParseError on valid JSON", () => {
		let called = false;
		parseReviewOutput(JSON.stringify({ approved: false }), () => { called = true; });
		expect(called).toBe(false);
	});

	it("defaults missing issues/suggestions to empty arrays", () => {
		const r = parseReviewOutput(JSON.stringify({ approved: true }));
		expect(r.issues).toEqual([]);
		expect(r.suggestions).toEqual([]);
	});
});
