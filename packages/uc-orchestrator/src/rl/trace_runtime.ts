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

const MAX_LIKELIHOODS_PER_SUBTASK = 64;
const MAX_SUBTASKS_PER_TASK = 16;
const MAX_TASKS = 64;

/** One subtask's observed likelihoods and derived credits. */
export interface SubtaskTrace {
	subtaskId: string;
	likelihoods: number[];
	credits: TurnCredit[];
}

interface SubtaskBuffer {
	order: string[];
	values: Map<string, number[]>;
}

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

/** Bounded per-task, per-subtask likelihood buffer owned by the orchestrator. */
export class TraceRuntimeCollector {
	private readonly byTask = new Map<string, SubtaskBuffer>();
	private readonly taskOrder: string[] = [];

	/**
	 * Record one progress payload.
	 *
	 * A task is a DAG: likelihoods from different subtasks belong to different
	 * trajectories, so they are kept in separate buckets. Concatenating them
	 * would compute credits across an artificial turn boundary.
	 */
	record(taskId: string, value: unknown, subtaskId = ""): void {
		const id = String(taskId ?? "").trim();
		if (!id) return;
		const values = normaliseTurnLikelihoods(value);
		if (values === undefined) return;

		let task = this.byTask.get(id);
		if (task === undefined) {
			task = { order: [], values: new Map() };
			this.byTask.set(id, task);
			this.taskOrder.push(id);
			this.evictOldestTasks();
		}
		const key = String(subtaskId ?? "").trim() || "__task__";
		let stored = task.values.get(key);
		if (stored === undefined) {
			stored = [];
			task.values.set(key, stored);
			task.order.push(key);
			this.evictOldestSubtasks(task);
		}
		for (const likelihood of values) {
			if (stored.length >= MAX_LIKELIHOODS_PER_SUBTASK) break;
			stored.push(likelihood);
		}
	}

	/** Copy the observed likelihoods for one subtask, or all subtasks (default). */
	likelihoods(taskId: string, subtaskId = ""): number[] {
		const task = this.byTask.get(taskId);
		if (task === undefined) return [];
		const key = String(subtaskId ?? "").trim() || "__task__";
		if (subtaskId) return [...(task.values.get(key) ?? [])];
		return task.order.flatMap((id) => [...(task.values.get(id) ?? [])]);
	}

	/** Per-subtask copies with credits computed independently. */
	creditsBySubtask(taskId: string): SubtaskTrace[] {
		const task = this.byTask.get(taskId);
		if (task === undefined) return [];
		return task.order.map((subtaskId) => {
			const likelihoods = [...(task.values.get(subtaskId) ?? [])];
			return {
				subtaskId,
				likelihoods,
				credits: likelihoods.length >= 2 ? computeTurnCredits(likelihoods) : [],
			};
		});
	}

	/** Compute TRACE turn credits per subtask and flatten in arrival order. */
	credits(taskId: string): TurnCredit[] {
		return this.creditsBySubtask(taskId).flatMap((row) => row.credits);
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

	private evictOldestSubtasks(task: SubtaskBuffer): void {
		while (task.order.length > MAX_SUBTASKS_PER_TASK) {
			const oldest = task.order.shift();
			if (oldest !== undefined) task.values.delete(oldest);
		}
	}
}
