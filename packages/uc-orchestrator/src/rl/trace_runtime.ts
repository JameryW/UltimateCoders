/**
 * TRACE (#20) runtime collection for the orchestrator.
 *
 * The pure credit algorithm lives in `trace_credit.ts`. This adapter is the
 * bounded collector between the worker's `subtask_progress` telemetry and that
 * algorithm: the worker only emits `turn_likelihoods` when an adapter actually
 * observed token logprobs, so the collector never invents a likelihood.
 *
 * Likelihoods arrive through the gateway's string-map event payload and may be
 * either a JSON-encoded array or an already-decoded array in local tests.
 */

import { computeTurnCredits, type TurnCredit } from "./trace_credit";

/** Environment flag shared with the worker's logprob-producing adapter. */
export const TRACE_LOGPROBS_ENV = "UC_TRACE_LOGPROBS";

const MAX_LIKELIHOODS_PER_TASK = 64;
const MAX_TASKS = 64;

/** Return whether runtime TRACE collection is enabled. */
export function traceLogprobsEnabled(): boolean {
	const value = process.env[TRACE_LOGPROBS_ENV];
	return value !== undefined && ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
}

/**
 * Normalize a worker-provided likelihood payload.
 *
 * Returns `undefined` when no usable observation exists. Invalid entries are
 * dropped rather than replaced with a default; a list that becomes empty is
 * treated as "no data", never as probability 1.
 */
export function normaliseTurnLikelihoods(value: unknown): number[] | undefined {
	let raw: unknown = value;
	if (typeof raw === "string") {
		const text = raw.trim();
		if (!text) return undefined;
		try {
			raw = JSON.parse(text);
		} catch {
			return undefined;
		}
	}
	if (!Array.isArray(raw)) return undefined;
	const values: number[] = [];
	for (const item of raw) {
		if (typeof item !== "number" || !Number.isFinite(item)) continue;
		if (item <= 0 || item > 1) continue;
		values.push(item);
	}
	return values.length > 0 ? values : undefined;
}

/** Bounded per-task likelihood buffer owned by the orchestrator. */
export class TraceRuntimeCollector {
	private readonly byTask = new Map<string, number[]>();
	private readonly taskOrder: string[] = [];

	/** Record one progress payload, ignoring malformed or disabled input. */
	record(taskId: string, value: unknown): void {
		const id = String(taskId ?? "").trim();
		if (!id) return;
		const values = normaliseTurnLikelihoods(value);
		if (values === undefined) return;

		let stored = this.byTask.get(id);
		if (stored === undefined) {
			stored = [];
			this.byTask.set(id, stored);
			this.taskOrder.push(id);
			this.evictOldestTasks();
		}
		for (const likelihood of values) {
			if (stored.length >= MAX_LIKELIHOODS_PER_TASK) break;
			stored.push(likelihood);
		}
	}

	/** Copy of the observed likelihoods for a task. */
	likelihoods(taskId: string): number[] {
		return [...(this.byTask.get(taskId) ?? [])];
	}

	/** Compute TRACE turn credits from the observed likelihoods. */
	credits(taskId: string): TurnCredit[] {
		const likelihoods = this.likelihoods(taskId);
		return likelihoods.length >= 2 ? computeTurnCredits(likelihoods) : [];
	}

	/** Release a completed task's buffer. */
	clear(taskId: string): void {
		this.byTask.delete(taskId);
		const index = this.taskOrder.indexOf(taskId);
		if (index >= 0) this.taskOrder.splice(index, 1);
	}

	/** Number of tasks currently buffered. */
	get size(): number {
		return this.byTask.size;
	}

	private evictOldestTasks(): void {
		while (this.taskOrder.length > MAX_TASKS) {
			const oldest = this.taskOrder.shift();
			if (oldest !== undefined) this.byTask.delete(oldest);
		}
	}
}
