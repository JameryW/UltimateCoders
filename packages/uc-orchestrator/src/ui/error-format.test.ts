/**
 * Tests for error-format helpers — classifyError and formatErrorForDisplay.
 *
 * Run: bun test src/ui/error-format.test.ts
 */

import { describe, expect, it } from "bun:test";
import type { ThemeColor } from "@oh-my-pi/pi-coding-agent";
import { classifyError, formatErrorForDisplay } from "./error-format";

describe("classifyError", () => {
	it("classifies 503 transient error from friendly Chinese summary", () => {
		const result = classifyError("LLM 瞬时错误（已重试 5 次）: 503 The system is busy");
		expect(result.kind).toBe("transient");
		expect(result.retryCount).toBe(5);
		expect(result.rootCause).toContain("503");
		expect(result.rootCause).toContain("system is busy");
	});

	it("classifies 429 rate limit as transient", () => {
		const result = classifyError("LLM 瞬时错误（已重试 3 次）: 429 rate_limit exceeded");
		expect(result.kind).toBe("transient");
		expect(result.retryCount).toBe(3);
	});

	it("classifies 529 overloaded as transient", () => {
		const result = classifyError("LLM 瞬时错误（已重试 2 次）: 529 overloaded");
		expect(result.kind).toBe("transient");
	});

	it("classifies permanent error from friendly Chinese summary", () => {
		const result = classifyError("LLM 永久错误: 401 invalid_api_key");
		expect(result.kind).toBe("permanent");
		expect(result.retryCount).toBeNull();
		expect(result.rootCause).toContain("401");
	});

	it("classifies 403 forbidden as permanent", () => {
		const result = classifyError("LLM 永久错误: 403 forbidden");
		expect(result.kind).toBe("permanent");
	});

	it("classifies unknown error without markers", () => {
		const result = classifyError("Execution error: something went wrong");
		expect(result.kind).toBe("unknown");
		expect(result.retryCount).toBeNull();
	});

	it("classifies raw 503 string without friendly prefix", () => {
		const result = classifyError("503 service unavailable");
		expect(result.kind).toBe("transient");
	});

	it("classifies raw 401 string without friendly prefix", () => {
		const result = classifyError("401 unauthorized");
		expect(result.kind).toBe("permanent");
	});

	it("extracts retry count from English 'retried N times' pattern", () => {
		const result = classifyError("transient error (retried 7 times): 503 busy");
		expect(result.retryCount).toBe(7);
	});

	it("returns null retryCount when no retry pattern found", () => {
		const result = classifyError("503 system is busy");
		expect(result.retryCount).toBeNull();
	});

	it("extracts root cause after last colon", () => {
		const result = classifyError("LLM 瞬时错误（已重试 1 次）: Error: 503 busy");
		expect(result.rootCause).toBe("503 busy");
	});
});

describe("formatErrorForDisplay", () => {
	const identity = (_color: ThemeColor, text: string) => text;

	it("formats transient error with retry count label", () => {
		const output = formatErrorForDisplay(
			"LLM 瞬时错误（已重试 5 次）: 503 The system is busy",
			100,
			identity,
		);
		expect(output).toContain("瞬时错误");
		expect(output).toContain("已重试5次");
		expect(output).toContain("503");
	});

	it("formats permanent error without retry count", () => {
		const output = formatErrorForDisplay(
			"LLM 永久错误: 401 invalid_api_key",
			100,
			identity,
		);
		expect(output).toContain("永久错误");
		expect(output).not.toContain("重试");
		expect(output).toContain("401");
	});

	it("formats unknown error with plain warning prefix", () => {
		const output = formatErrorForDisplay(
			"Execution error: disk full on /tmp",
			100,
			identity,
		);
		expect(output).toContain("⚠");
		expect(output).toContain("disk full");
	});

	it("truncates root cause, not the friendly prefix", () => {
		const longCause = "503 " + "x".repeat(200);
		const output = formatErrorForDisplay(
			`LLM 瞬时错误（已重试 3 次）: ${longCause}`,
			20,
			identity,
		);
		// The friendly prefix "⚠ 瞬时错误 (已重试3次): " should be preserved
		expect(output).toContain("瞬时错误");
		expect(output).toContain("已重试3次");
		// Root cause should be truncated with ellipsis
		expect(output).toContain("…");
		expect(output.length).toBeLessThan(longCause.length);
	});

	it("does not truncate short root causes", () => {
		const output = formatErrorForDisplay(
			"LLM 永久错误: 401 bad key",
			100,
			identity,
		);
		expect(output).not.toContain("…");
		expect(output).toContain("401 bad key");
	});

	it("applies color function to output", () => {
		let colorCalled = false;
		const coloredFn = (color: ThemeColor, text: string) => {
			colorCalled = true;
			return `<${color}>${text}</${color}>`;
		};
		const output = formatErrorForDisplay("503 busy", 100, coloredFn);
		expect(colorCalled).toBe(true);
		expect(output).toContain("<error>");
		expect(output).toContain("</error>");
	});
});
