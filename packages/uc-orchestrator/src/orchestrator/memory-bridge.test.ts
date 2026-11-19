/**
 * Memory-bridge preview contract.
 *
 * Tool results used to cut content with a silent `.slice(0, N)`, so the model
 * could not tell "this is all there was" from "the rest was dropped" and had
 * no way to ask for the missing part. These checks pin the recoverable-marker
 * behaviour: short text is untouched, long text keeps its head plus an
 * explicit marker naming the limit, the dropped count, and how to recover.
 *
 * Run: bun test src/orchestrator/memory-bridge.test.ts
 */

import { describe, expect, it } from "bun:test";
import {
	MEMORY_EXCERPT_CHARS,
	SEARCH_SNIPPET_CHARS,
	excerptWithMarker,
	formatMemorySearchLine,
	formatSearchResultLine,
} from "./memory-bridge";

describe("excerptWithMarker", () => {
	it("returns short text untouched", () => {
		expect(excerptWithMarker("short", 200, "read it")).toBe("short");
		expect(excerptWithMarker("exactly", 7, "read it")).toBe("exactly");
	});

	it("keeps the head and names the limit, the loss, and the way back", () => {
		const text = "x".repeat(500);
		const out = excerptWithMarker(text, 200, "call uc_memory again");
		expect(out.startsWith("x".repeat(200))).toBe(true);
		expect(out).toContain("truncated: 200/500 chars shown, 300 omitted");
		expect(out).toContain("call uc_memory again");
		expect(out.length).toBeGreaterThan(200);
	});
});

describe("result line formatting", () => {
	it("marks a cut memory entry instead of silently slicing it", () => {
		const line = formatMemorySearchLine({
			content: "y".repeat(MEMORY_EXCERPT_CHARS + 25),
			score: 0.87,
		});
		expect(line.startsWith("[0.87] ")).toBe(true);
		expect(line).toContain(`truncated: ${MEMORY_EXCERPT_CHARS}/`);
		expect(line).toContain("omitted");
	});

	it("leaves a short memory entry intact", () => {
		expect(formatMemorySearchLine({ content: "a fact", score: 0.5 })).toBe("[0.50] a fact");
	});

	it("marks a cut code snippet with the file path and score", () => {
		const line = formatSearchResultLine({
			filePath: "src/app.ts",
			snippet: "z".repeat(SEARCH_SNIPPET_CHARS + 10),
			score: 0.4,
		});
		expect(line.startsWith("src/app.ts (score=0.40): ")).toBe(true);
		expect(line).toContain(`truncated: ${SEARCH_SNIPPET_CHARS}/`);
		expect(line).toContain("open the file at that path");
	});
});
