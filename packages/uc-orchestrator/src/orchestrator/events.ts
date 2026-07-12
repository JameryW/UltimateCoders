/**
 * UC Orchestrator Events — internal event emitter for decoupling
 * orchestration logic from presentation.
 *
 * Serves two channels:
 * 1. OMP TUI extension (rich Component rendering)
 * 2. JSONL stdout (structured event payloads for Python bridge)
 */

import type { TaskState, SubtaskResult } from "./orchestrator";

// ── Event Types ──────────────────────────────────────────────────

export interface OrchestratorEvents {
	/** Task entered planning phase */
	task_planning: { taskId: string; description: string };

	/** Task decomposition complete, DAG built */
	task_decomposed: { taskId: string; subtaskCount: number; waveCount: number };

	/** A wave is starting */
	wave_start: { taskId: string; waveIdx: number; totalWaves: number; subtaskIds: string[] };

	/** A wave completed */
	wave_end: { taskId: string; waveIdx: number; totalWaves: number; results: SubtaskResult[] };

	/** A subtask started executing */
	subtask_start: { taskId: string; subtaskId: string; description: string };

	/** A subtask completed successfully */
	subtask_end: { taskId: string; subtaskId: string; result?: string };

	/** A subtask failed */
	subtask_failed: { taskId: string; subtaskId: string; error?: string; retryCount?: number };

	/** A subtask is being reviewed */
	subtask_reviewing: { taskId: string; subtaskId: string };

	/** Real-time subtask execution progress (phase/percent/agent) */
	subtask_progress: {
		taskId: string;
		subtaskId: string;
		workerId: string;
		phase: string;
		percent: number;
		stepIndex?: number;
		stepTotal?: number;
		stepAgent?: string;
		stepStatus?: string;
		stepSummary?: string;
		parallelGroup?: string;
		parallelStepCount?: number;
	};

	/** Task reached terminal state */
	task_complete: { taskId: string; status: TaskState["status"]; summary: string };

	/** Task paused */
	task_paused: { taskId: string; waveIdx: number };

	/** Task resumed */
	task_resumed: { taskId: string };

	/** Task cancelled */
	task_cancelled: { taskId: string };

	/** Connection state changed (gRPC bridge) */
	connection_state: { connected: boolean; error?: string };
}

export type OrchestratorEventType = keyof OrchestratorEvents;
export type OrchestratorEventHandler<E extends OrchestratorEventType> = (
	event: OrchestratorEvents[E],
) => void;

// ── Typed EventEmitter ───────────────────────────────────────────

// ponytail: minimal typed emitter — Node EventEmitter is overkill for <10 listeners
export class OrchestratorEventEmitter {
	private handlers = new Map<OrchestratorEventType, Set<OrchestratorEventHandler<any>>>();

	on<E extends OrchestratorEventType>(event: E, handler: OrchestratorEventHandler<E>): () => void {
		let set = this.handlers.get(event);
		if (!set) {
			set = new Set();
			this.handlers.set(event, set);
		}
		set.add(handler);
		// Return unsubscribe function
		return () => set!.delete(handler);
	}

	emit<E extends OrchestratorEventType>(event: E, payload: OrchestratorEvents[E]): void {
		const set = this.handlers.get(event);
		if (set) {
			for (const handler of set) {
				try {
					handler(payload);
				} catch (err) {
					console.warn(`OrchestratorEventEmitter handler error for ${event}: ${err}`);
				}
			}
		}
	}

	/** Remove all handlers. Called on session shutdown. */
	clear(): void {
		this.handlers.clear();
	}
}
