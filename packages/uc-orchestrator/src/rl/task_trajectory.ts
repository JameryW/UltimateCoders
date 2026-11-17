/**
 * SWE-Prime (#11) runtime trajectory collection for the orchestrator.
 *
 * The training-side `swe_prime` module operates on semantic segments. This
 * adapter turns the progress events the orchestrator already receives into a
 * bounded, deterministic segment stream so task completion can produce a real
 * loss mask instead of an unused library value.
 *
 * The mapping is deliberately conservative: a progress phase is a segment
 * boundary, the event sequence is its turn, and only explicit rollback or a
 * repeated ineffective step is masked. The result is a diagnostic/data-
 * collection signal, not a training run.
 */

import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

import {
	distillSegments,
	type DistillOptions,
	type DistilledTrajectory,
	type SegmentKind,
	type TrajectorySegment,
} from "./swe_prime";

/** Environment flag for runtime trajectory distillation. */
export const SWE_PRIME_ENV = "UC_SWE_PRIME";

/** Optional directory for segment-level SWE-Prime export. */
export const SWE_PRIME_EXPORT_DIR_ENV = "UC_SWE_PRIME_EXPORT_DIR";
const EXPORT_FILE = "swe-prime-trajectories.jsonl";

const MAX_EVENTS_PER_TASK = 800;
const MAX_TASKS = 64;
const MAX_PHASE_CHARS = 200;
const MAX_SUMMARY_CHARS = 300;

/** Return whether runtime trajectory distillation is enabled. */
export function swePrimeEnabled(): boolean {
	const value = process.env[SWE_PRIME_ENV];
	return value !== undefined && ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
}

/** Return the configured segment-export directory, or null when disabled. */
export function swePrimeExportDir(): string | null {
	const value = process.env[SWE_PRIME_EXPORT_DIR_ENV];
	const directory = (value ?? "").trim();
	return directory.length > 0 ? directory : null;
}

/**
 * Append one distilled task to the segment-level SWE-Prime JSONL export.
 *
 * This is deliberately *not* token-level TITO: it exports the semantic
 * segments and loss mask the orchestrator can observe. Providers that do not
 * expose token logprobs still produce a usable segment record. Returns the
 * file path written, or null when there are no events.
 */
export function exportTaskTrajectory(
	taskId: string,
	events: readonly TrajectoryProgressEvent[],
	options: DistillOptions,
	directory: string,
): string | null {
	const task = taskId.trim();
	if (!task) throw new TypeError("taskId must be a non-empty string");
	const root = directory.trim();
	if (!root) throw new TypeError("directory must be a non-empty string");
	const segments = segmentsFromProgress(events);
	if (segments.length === 0) return null;
	const distilled = distillSegments(segments, options);
	const record = {
		v: 1,
		task_id: task,
		exported_at: new Date().toISOString(),
		kept_ratio: distilled.keptRatio,
		loss_mask: distilled.lossMask,
		segments: distilled.segments,
	};
	mkdirSync(root, { recursive: true });
	const path = join(root, EXPORT_FILE);
	appendFileSync(path, `${JSON.stringify(record)}\n`, "utf8");
	return path;
}

/** Minimal progress event shape emitted by the orchestrator. */
export interface TrajectoryProgressEvent {
	taskId: string;
	subtaskId?: string;
	phase: string;
	stepIndex?: number;
	stepStatus?: string;
	stepSummary?: string;
}

/** A compact description suitable for a log line or an event payload. */
export interface TaskTrajectorySummary {
	taskId: string;
	segments: number;
	keptSegments: number;
	keptRatio: number;
	lossMask: number[];
	kinds: SegmentKind[];
}

const PHASE_RULES: ReadonlyArray<{ pattern: RegExp; kind: SegmentKind }> = [
	{ pattern: /revert|rollback|reverted/i, kind: "repair" },
	{ pattern: /explor|survey|scan|search|understand/i, kind: "exploration" },
	{ pattern: /localiz|locate|inspect|read|find|grep/i, kind: "localization" },
	{ pattern: /edit|write|patch|apply|implement/i, kind: "editing" },
	{ pattern: /verif|test|lint|build|check/i, kind: "verification" },
	{ pattern: /repair|fix|retry|diagnos/i, kind: "repair" },
];

/** Map one worker progress phase to a SWE-Prime segment kind. */
export function phaseKind(phase: string): SegmentKind {
	const text = phase.trim();
	for (const rule of PHASE_RULES) {
		if (rule.pattern.test(text)) return rule.kind;
	}
	return "exploration";
}

function normaliseText(value: string | undefined, limit: number): string {
	return (value ?? "").replace(/\s+/g, " ").trim().slice(0, limit);
}

function isRevertPhase(phase: string): boolean {
	return /revert|rollback|reverted/i.test(phase);
}

function isCompleted(status: string | undefined): boolean {
	const value = (status ?? "").trim().toLowerCase();
	return value === "completed" || value === "done" || value === "success";
}

/**
 * Convert a task's progress events into semantic segments.
 *
 * Turns are the event positions in input order. A repeated `(kind, summary)`
 * pair keeps only its last occurrence as effective; earlier occurrences are
 * marked `repeatedIneffective`. A revert/rollback phase is marked `reverted`,
 * and a completed edit/verification segment is a positive core.
 */
export function segmentsFromProgress(
	events: readonly TrajectoryProgressEvent[],
): TrajectorySegment[] {
	if (!Array.isArray(events)) throw new TypeError("events must be an array");

	const segments: TrajectorySegment[] = events.map((event, turn) => {
		const phase = normaliseText(event.phase, MAX_PHASE_CHARS);
		const summary = normaliseText(event.stepSummary, MAX_SUMMARY_CHARS);
		const kind = phaseKind(phase);
		return {
			id: `${turn}`,
			kind,
			startTurn: turn,
			endTurn: turn,
			reverted: isRevertPhase(phase),
			repeatedIneffective: false,
			positiveCore:
				isCompleted(event.stepStatus) && (kind === "editing" || kind === "verification"),
			metadata: {
				phase,
				subtaskId: event.subtaskId ?? "",
				stepIndex: event.stepIndex,
				summary,
			},
		};
	});

	// Keep the last occurrence of an identical (kind, summary) pair; earlier
	// repeats add no information and must not receive loss.
	const lastIndex = new Map<string, number>();
	for (let index = 0; index < segments.length; index++) {
		const segment = segments[index];
		if (segment === undefined) continue;
		const summary = String(segment.metadata?.summary ?? "");
		if (summary.length === 0) continue;
		lastIndex.set(`${segment.kind}\u0000${summary}`, index);
	}
	for (let index = 0; index < segments.length; index++) {
		const segment = segments[index];
		if (segment === undefined) continue;
		const summary = String(segment.metadata?.summary ?? "");
		if (summary.length === 0) continue;
		if (lastIndex.get(`${segment.kind}\u0000${summary}`) !== index) {
			segment.repeatedIneffective = true;
		}
	}
	return segments;
}

/** Distil one task's progress events into a loss mask plus a compact summary. */
export function distillTaskTrajectory(
	taskId: string,
	events: readonly TrajectoryProgressEvent[],
	options: DistillOptions = {},
): TaskTrajectorySummary | null {
	const task = taskId.trim();
	if (!task) throw new TypeError("taskId must be a non-empty string");
	const segments = segmentsFromProgress(events);
	if (segments.length === 0) return null;
	const distilled: DistilledTrajectory = distillSegments(segments, options);
	return {
		taskId: task,
		segments: segments.length,
		keptSegments: distilled.segments.length,
		keptRatio: distilled.keptRatio,
		lossMask: distilled.lossMask,
		kinds: distilled.segments.map((segment) => segment.kind),
	};
}

/** Bounded per-task event buffer owned by the orchestrator. */
export class TaskTrajectoryCollector {
	private readonly eventsByTask = new Map<string, TrajectoryProgressEvent[]>();
	private readonly taskOrder: string[] = [];

	/** Record one progress event, ignoring malformed or unbounded input. */
	record(event: TrajectoryProgressEvent): void {
		if (event === null || typeof event !== "object") return;
		const taskId = String(event.taskId ?? "").trim();
		if (!taskId) return;
		let events = this.eventsByTask.get(taskId);
		if (events === undefined) {
			events = [];
			this.eventsByTask.set(taskId, events);
			this.taskOrder.push(taskId);
			this.evictOldestTasks();
		}
		if (events.length >= MAX_EVENTS_PER_TASK) return;
		events.push({
			taskId,
			subtaskId: event.subtaskId,
			phase: normaliseText(event.phase, MAX_PHASE_CHARS),
			stepIndex: event.stepIndex,
			stepStatus: event.stepStatus,
			stepSummary: normaliseText(event.stepSummary, MAX_SUMMARY_CHARS),
		});
	}

	/** Distil one task, or return null when no progress was recorded. */
	distill(taskId: string, options: DistillOptions = {}): TaskTrajectorySummary | null {
		const events = this.eventsByTask.get(taskId);
		if (events === undefined || events.length === 0) return null;
		return distillTaskTrajectory(taskId, events, options);
	}

	/** Export one task's semantic trajectory without clearing the buffer. */
	export(
		taskId: string,
		directory: string,
		options: DistillOptions = {},
	): string | null {
		const events = this.eventsByTask.get(taskId);
		if (events === undefined || events.length === 0) return null;
		return exportTaskTrajectory(taskId, events, options, directory);
	}

	/** Release a completed task's buffer. */
	clear(taskId: string): void {
		this.eventsByTask.delete(taskId);
		const index = this.taskOrder.indexOf(taskId);
		if (index >= 0) this.taskOrder.splice(index, 1);
	}

	/** Number of tasks currently buffered. */
	get size(): number {
		return this.eventsByTask.size;
	}

	private evictOldestTasks(): void {
		while (this.taskOrder.length > MAX_TASKS) {
			const oldest = this.taskOrder.shift();
			if (oldest !== undefined) this.eventsByTask.delete(oldest);
		}
	}
}
