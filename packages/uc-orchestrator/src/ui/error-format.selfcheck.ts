/**
 * Self-check for error-format helpers (classifyError + formatErrorForDisplay).
 * Run: bun run src/ui/error-format.selfcheck.ts
 *
 * ponytail: error-format is core logic used by 4+ UI components (progress-widget,
 * status-formatter, task-result-renderer, subtask-tree). It classifies errors
 * (transient/permanent/unknown) via markers + status-code regex, extracts the
 * root cause + retry count, and truncates. A parser/classifier with no check is
 * the exact "silent failure" risk ponytail flags — pin it.
 */
import { classifyError, formatErrorForDisplay, type ErrorKind } from "./error-format";

let failures = 0;
function check(name: string, cond: boolean) {
	console.log(`${cond ? "PASS" : "FAIL"}: ${name}`);
	if (!cond) failures++;
}

// identity fg so the output is plain text (assert on substrings)
const fg = (_c: unknown, t: string) => t;

// ── classification: transient markers ────────────────────────────
// Chinese (worker friendly summary) + English markers
check("transient: 瞬时错误 marker", classifyError("LLM 瞬时错误（已重试 5 次）: 503 busy").kind === "transient");
check("transient: 'transient' marker", classifyError("transient error: overloaded").kind === "transient");
check("transient: 'overloaded' marker", classifyError("server overloaded").kind === "transient");
check("transient: 'rate_limit' marker", classifyError("rate_limit exceeded").kind === "transient");
check("transient: 503 code", classifyError("HTTP 503 service unavailable").kind === "transient");
check("transient: 429 code", classifyError("429 Too Many Requests").kind === "transient");
check("transient: 529 code", classifyError("529 overloaded").kind === "transient");

// ── classification: permanent markers ────────────────────────────
check("permanent: 永久错误 marker", classifyError("LLM 永久错误: 401 invalid_api_key").kind === "permanent");
check("permanent: 'invalid_api_key' marker", classifyError("invalid_api_key supplied").kind === "permanent");
check("permanent: 'unauthorized' marker", classifyError("unauthorized access").kind === "permanent");
check("permanent: 401 code", classifyError("401 Unauthorized").kind === "permanent");
check("permanent: 403 code", classifyError("403 Forbidden").kind === "permanent");
check("permanent: 404 code", classifyError("404 Not Found").kind === "permanent");

// ── classification: unknown (no markers) ─────────────────────────
check("unknown: plain execution error", classifyError("Execution error: something broke").kind === "unknown");
check("unknown: empty string", classifyError("").kind === "unknown");

// ── F23: bare status codes match as whole tokens only ───────────
// lookarounds reject DIGIT/dot adjacency. "processed400" (digit-adjacent) and
// "404.html" (dot-adjacent) are rejected; a space-delimited 400 IS a whole token
// and classifies as permanent (400 is a known status) — that's the intended
// "whole token" behavior, the F23 comment's "processed 400 items" example was
// imprecise (400 there is space-delimited, a real token).
check("F23: digit-adjacent 400 not permanent", classifyError("processed400 items").kind === "unknown");
check("F23: 'port 8429' not transient", classifyError("connecting to port 8429").kind === "unknown");
check("F23: '404.html' not permanent", classifyError("served 404.html").kind === "unknown");
check("F23: space-delimited 400 IS permanent (whole token)", classifyError("processed 400 items").kind === "permanent");

// ── retry count extraction ───────────────────────────────────────
check("retry: 已重试 N 次", classifyError("瞬时错误（已重试 3 次）: x").retryCount === 3);
check("retry: retried N times", classifyError("transient (retried 7 times): x").retryCount === 7);
check("retry: none when absent", classifyError("503 busy").retryCount === null);
check("retry: none on permanent", classifyError("401 invalid key").retryCount === null);

// ── root cause extraction ────────────────────────────────────────
// everything after the last ": " that follows a friendly marker
check("rootCause: after ': '", classifyError("LLM 瞬时错误: 503 the system is busy").rootCause === "503 the system is busy");
check("rootCause: whole string when no ': '", classifyError("just an error").rootCause === "just an error");
// F18: multi-line root cause flattened to one line (newline → space)
check("rootCause: newlines flattened", classifyError("err: line1\nline2\n  line3").rootCause === "line1 line2 line3");

// ── formatErrorForDisplay: labels + truncation ───────────────────
const longCause = "x".repeat(120);
const transientLong = `瞬时错误: ${longCause}`;
const out = formatErrorForDisplay(transientLong, 40, fg);
check("format transient: ⚠ 瞬时错误 label", out.includes("⚠ 瞬时错误"));
check("format transient: retry label when present", formatErrorForDisplay("瞬时错误（已重试 2 次）: x", 80, fg).includes("已重试2次"));
check("format transient: truncated with ellipsis", out.includes("…") && !out.includes("x".repeat(40)));
check("format transient: warning color (plain, just present)", out.includes("⚠"));

const permanentOut = formatErrorForDisplay(`永久错误: ${longCause}`, 40, fg);
check("format permanent: ⚠ 永久错误 label", permanentOut.includes("⚠ 永久错误"));
check("format permanent: truncated", permanentOut.includes("…") && !permanentOut.includes("x".repeat(40)));

const unknownOut = formatErrorForDisplay(`Execution error: ${longCause}`, 40, fg);
check("format unknown: ⚠ label only (no kind word)", unknownOut.includes("⚠") && !unknownOut.includes("瞬时错误") && !unknownOut.includes("永久错误"));

// narrow width clamps to empty root cause (no room) but keeps the label
check("format: width 0 → label only, no ellipsis body", formatErrorForDisplay("永久错误: x", 0, fg).includes("⚠ 永久错误"));
// fits within width → no ellipsis
check("format: fits width → no ellipsis", !formatErrorForDisplay("err: short", 80, fg).includes("…"));

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
