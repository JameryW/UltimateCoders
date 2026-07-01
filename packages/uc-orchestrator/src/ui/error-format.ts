/**
 * Error format helpers — classify and render LLM error strings.
 *
 * The Worker produces structured friendly summaries like:
 *   "LLM 瞬时错误（已重试 5 次）: 503 The system is busy..."
 *   "LLM 永久错误: 401 invalid_api_key"
 *   "Execution error: <raw>"
 *
 * These helpers detect the transient/permanent markers in those strings so
 * UI renderers can show a colored label + icon and truncate the root cause
 * (not the friendly prefix).
 */

// ponytail: markers must stay in sync with python/ultimate_coders/agent/llm.py
// _TRANSIENT_RETRY_MARKERS and _PERMANENT_ERROR_MARKERS.
const TRANSIENT_MARKERS = [
	"瞬时错误", // "transient error" in Chinese (from worker.py friendly summary)
	"transient",
	"503",
	"429",
	"529",
	"overloaded",
	"server_error",
	"system is busy",
	"try again later",
	"service unavailable",
	"rate_limit",
];

const PERMANENT_MARKERS = [
	"永久错误", // "permanent error" in Chinese (from worker.py friendly summary)
	"permanent",
	"400",
	"401",
	"403",
	"404",
	"invalid_api_key",
	"invalid key",
	"unauthorized",
	"forbidden",
	"authentication",
];

export type ErrorKind = "transient" | "permanent" | "unknown";

export interface ClassifiedError {
	kind: ErrorKind;
	/** The root-cause portion of the error (after the friendly prefix). */
	rootCause: string;
	/** Retry count extracted from the message, if present. */
	retryCount: number | null;
}

/**
 * Classify an error string as transient, permanent, or unknown.
 * Extracts the root cause (strips the friendly prefix) and retry count.
 */
export function classifyError(errorStr: string): ClassifiedError {
	const lower = errorStr.toLowerCase();

	let kind: ErrorKind = "unknown";
	if (TRANSIENT_MARKERS.some((m) => lower.includes(m.toLowerCase()))) {
		kind = "transient";
	} else if (PERMANENT_MARKERS.some((m) => lower.includes(m.toLowerCase()))) {
		kind = "permanent";
	}

	// Extract retry count from "已重试 N 次" or "retried N times" patterns
	let retryCount: number | null = null;
	const retryMatch = errorStr.match(/(?:已重试|retried)\s*(\d+)\s*(?:次|times)/i);
	if (retryMatch) {
		retryCount = parseInt(retryMatch[1], 10);
	}

	// Extract root cause: everything after the last ":" that follows a
	// friendly marker. If no marker found, return the whole string.
	const colonIdx = errorStr.lastIndexOf(": ");
	let rootCause = errorStr;
	if (colonIdx > 0 && colonIdx < errorStr.length - 2) {
		rootCause = errorStr.slice(colonIdx + 2);
	}

	return { kind, rootCause, retryCount };
}

/**
 * Format an error string for display with a colored label.
 * Truncates the root cause (not the friendly prefix) to fit maxWidth.
 *
 * @param errorStr The raw error string (from st.error or result.summary).
 * @param maxWidth Maximum display width for the root-cause portion.
 * @param fgColored Function to colorize text: (color, text) => string.
 * @returns Colored, truncated error string ready for display.
 */
export function formatErrorForDisplay(
	errorStr: string,
	maxWidth: number,
	fgColored: (color: string, text: string) => string,
): string {
	const { kind, rootCause, retryCount } = classifyError(errorStr);

	const truncated = rootCause.length > maxWidth
		? rootCause.slice(0, maxWidth - 1) + "…"
		: rootCause;

	if (kind === "transient") {
		const retryLabel = retryCount !== null ? ` (已重试${retryCount}次)` : "";
		return fgColored("error", `⚠ 瞬时错误${retryLabel}: ${truncated}`);
	}
	if (kind === "permanent") {
		return fgColored("error", `⚠ 永久错误: ${truncated}`);
	}
	return fgColored("error", `⚠ ${truncated}`);
}
