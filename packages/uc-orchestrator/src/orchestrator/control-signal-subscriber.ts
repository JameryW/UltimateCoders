/**
 * ControlSignalSubscriber — Subscribes to NATS control events (pause/resume/cancel)
 * and invokes the corresponding Orchestrator methods.
 *
 * When NATS is unavailable, starts a polling fallback that checks the gRPC
 * TaskStore for controlState changes on active tasks.
 *
 * Architecture:
 *   gRPC PauseTask/ResumeTask/CancelTask → NATS uc.task.event → this subscriber → Orchestrator
 *   Fallback: poll GrpcBridge.getTask() every 2s when NATS disconnected
 */

import { connect, type NatsConnection, type Subscription } from "nats";
import { GrpcBridge, type TaskSync } from "./grpc-bridge";

// ── Types ──────────────────────────────────────────────────────────

export interface ControlSignalHandler {
	pauseTask(taskId: string): Promise<boolean>;
	resumeTask(taskId: string): Promise<boolean>;
	cancelTask(taskId: string): Promise<boolean>;
	/** Get IDs of currently active (non-terminal) tasks. */
	getActiveTaskIds(): string[];
}

export interface ControlSignalSubscriberConfig {
	/** NATS server URL. Default: nats://localhost:4222 */
	natsUrl: string;
	/** gRPC server URL for polling fallback. Default: http://localhost:50051 */
	grpcUrl: string;
	/** Polling interval (ms) when NATS is unavailable. Default: 2000 */
	pollIntervalMs: number;
}

const DEFAULT_CONFIG: ControlSignalSubscriberConfig = {
	natsUrl: process.env.UC_NATS_URL ?? "nats://localhost:4222",
	grpcUrl: process.env.GRPC_SERVER_ADDR
		? `http://${process.env.GRPC_SERVER_ADDR}`
		: "http://localhost:50051",
	pollIntervalMs: 2000,
};

// ── NATS Event Payload ─────────────────────────────────────────────

interface NatsControlEvent {
	/** Deduplication key. */
	message_id?: string;
	/** Event type: task_paused, task_resumed, task_cancelled */
	type: string;
	/** Task ID. */
	task_id: string;
}

// ── Subscriber ─────────────────────────────────────────────────────

export class ControlSignalSubscriber {
	private handler: ControlSignalHandler;
	private config: ControlSignalSubscriberConfig;
	private natsConn: NatsConnection | null = null;
	private eventSub: Subscription | null = null;
	private pollTimer: ReturnType<typeof setInterval> | null = null;
	private natsConnected = false;
	/** Track last known controlState per task to detect changes via polling. */
	private lastKnownControlState: Map<string, string> = new Map();
	/** Dedup: seen message IDs to prevent double-processing. */
	private seenMessageIds: Map<string, number> = new Map();

	constructor(handler: ControlSignalHandler, config?: Partial<ControlSignalSubscriberConfig>) {
		this.handler = handler;
		this.config = { ...DEFAULT_CONFIG, ...config };
	}

	/** Start the subscriber. Tries NATS first; falls back to polling. */
	async start(): Promise<void> {
		try {
			this.natsConn = await connect({ servers: this.config.natsUrl, timeout: 2_000 });
			this.natsConnected = true;
			this.startNatsSubscription();
			console.info(`[ControlSignalSubscriber] Connected to NATS at ${this.config.natsUrl}`);
		} catch (err) {
			this.natsConnected = false;
			console.warn(
				`[ControlSignalSubscriber] NATS unavailable (${err instanceof Error ? err.message : String(err)}), starting polling fallback`,
			);
			this.startPolling();
		}
	}

	/** Stop the subscriber and clean up resources. */
	async stop(): Promise<void> {
		if (this.pollTimer) {
			clearInterval(this.pollTimer);
			this.pollTimer = null;
		}
		if (this.eventSub) {
			this.eventSub.unsubscribe();
			this.eventSub = null;
		}
		if (this.natsConn) {
			await this.natsConn.drain();
			this.natsConn = null;
		}
		this.natsConnected = false;
	}

	/** Whether NATS is currently connected. */
	isNatsConnected(): boolean {
		return this.natsConnected;
	}

	// ── NATS Subscription ────────────────────────────────────────

	private startNatsSubscription(): void {
		if (!this.natsConn) return;

		this.eventSub = this.natsConn.subscribe("uc.task.event");
		const sub = this.eventSub;
		(async () => {
			for await (const msg of sub) {
				try {
					const event = JSON.parse(new TextDecoder().decode(msg.data)) as NatsControlEvent;
					this.handleNatsEvent(event);
				} catch {
					// Malformed JSON — skip
				}
			}
		})().catch(() => {
			// Subscription ended
		});
	}

	private handleNatsEvent(event: NatsControlEvent): void {
		const { type, task_id, message_id } = event;

		// Only handle control events
		if (type !== "task_paused" && type !== "task_resumed" && type !== "task_cancelled") {
			return;
		}

		// Dedup
		if (message_id) {
			const now = Date.now();
			const lastSeen = this.seenMessageIds.get(message_id);
			if (lastSeen !== undefined && now - lastSeen < 300_000) {
				return; // Duplicate within 5 minutes
			}
			this.seenMessageIds.set(message_id, now);
			// Evict old entries
			if (this.seenMessageIds.size > 10_000) {
				for (const [id, ts] of this.seenMessageIds) {
					if (now - ts > 300_000) this.seenMessageIds.delete(id);
				}
			}
		}

		console.info(`[ControlSignalSubscriber] Received NATS control event: ${type} for task ${task_id}`);

		switch (type) {
			case "task_paused":
				this.handler.pauseTask(task_id).catch((err) => {
					console.warn(`[ControlSignalSubscriber] pauseTask failed for ${task_id}: ${err}`);
				});
				break;
			case "task_resumed":
				this.handler.resumeTask(task_id).catch((err) => {
					console.warn(`[ControlSignalSubscriber] resumeTask failed for ${task_id}: ${err}`);
				});
				break;
			case "task_cancelled":
				this.handler.cancelTask(task_id).catch((err) => {
					console.warn(`[ControlSignalSubscriber] cancelTask failed for ${task_id}: ${err}`);
				});
				break;
		}
	}

	// ── Polling Fallback ────────────────────────────────────────

	private startPolling(): void {
		const bridge = new GrpcBridge({ serverUrl: this.config.grpcUrl });

		this.pollTimer = setInterval(async () => {
			const activeIds = this.handler.getActiveTaskIds();
			if (activeIds.length === 0) return;

			for (const taskId of activeIds) {
				try {
					const task = await bridge.getTask(taskId);
					if (!task) continue;
					this.checkControlStateChange(taskId, task);
				} catch {
					// gRPC unreachable — skip
				}
			}
		}, this.config.pollIntervalMs);
	}

	/**
	 * Check if the gRPC TaskStore's task state differs from our local knowledge.
	 * If a control signal is detected (paused/cancelled), invoke the handler.
	 *
	 * Note: gRPC cancel_task sets status to "Failed", which is indistinguishable
	 * from a natural failure via polling alone. We guard against false positives
	 * by only treating a "Failed" transition as a cancel if the Orchestrator
	 * still considers the task active (not already failed/cancelled locally).
	 * When NATS is available, the exact event_type disambiguates automatically.
	 */
	private checkControlStateChange(taskId: string, task: TaskSync): void {
		const currentStatus = task.status;
		const previous = this.lastKnownControlState.get(taskId);

		// Update our tracking
		this.lastKnownControlState.set(taskId, currentStatus);

		// If no previous state, just record it
		if (!previous) return;

		// Detect transitions
		if (currentStatus === "Paused" && previous !== "Paused") {
			console.info(`[ControlSignalSubscriber] Polling detected pause for task ${taskId}`);
			this.handler.pauseTask(taskId).catch(() => {});
		} else if (currentStatus === "Failed" && previous !== "Failed") {
			// gRPC cancel_task sets status to Failed. Distinguish from natural
			// failure by checking whether the Orchestrator still considers the
			// task active — if so, the failure must have come from gRPC (cancel).
			const activeIds = this.handler.getActiveTaskIds();
			if (activeIds.includes(taskId)) {
				console.info(`[ControlSignalSubscriber] Polling detected cancel (Failed) for active task ${taskId}`);
				this.handler.cancelTask(taskId).catch(() => {});
			} else {
				console.info(`[ControlSignalSubscriber] Polling detected natural failure for task ${taskId} (already terminal locally)`);
			}
		}
	}
}
