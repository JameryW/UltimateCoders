/**
 * gRPC Bridge — Connects uc-orchestrator to UC Rust core engine.
 *
 * Uses gRPC-Web (application/grpc-web+proto) via @connectrpc/connect-web
 * to communicate with the tonic-web enabled UC gRPC server.
 *
 * ponytail: connectrpc client — proper gRPC-Web protocol, no hand-rolled framing.
 */

import { createClient } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import {
	EngineService,
	TaskService,
	DashboardService,
	WorkerService,
	HealthRequestSchema,
	SubmitTaskRequestSchema,
	GetTaskRequestSchema,
	ListTasksRequestSchema,
	UpdateTaskRequestSchema,
	PauseTaskRequestSchema,
	ResumeTaskRequestSchema,
	CancelTaskRequestSchema,
	ReadMemoryRequestSchema,
	WriteMemoryRequestSchema,
	DeleteMemoryRequestSchema,
	SearchMemoryRequestSchema,
	BatchWriteMemoryRequestSchema,
	SearchRequestSchema,
	IndexRepoRequestSchema,
	GetIndexStateRequestSchema,
	RemoveIndexRequestSchema,
	ListReposRequestSchema,
	ListDirRequestSchema,
	GetFileRequestSchema,
	ListWorkersRequestSchema,
	ScaleWorkersRequestSchema,
	WatchTaskRequestSchema,
	type SubmitTaskResponse,
	type TaskEvent,
} from "../grpc/engine_pb.js";
import { create } from "@bufbuild/protobuf";
import { readFileSync } from "node:fs";
import { sleepBackoff, type BackoffOptions } from "./backoff";

// ── Types ──────────────────────────────────────────────────────────

export interface BridgeConfig {
	/** gRPC server address. Default: "http://localhost:50051" */
	serverUrl: string;
	/** Request timeout (ms). Default: 10000 */
	timeoutMs: number;
	/** Callback when connection state changes. */
	onConnectionChange?: (connected: boolean) => void;
	/** Backoff config for connection-error reconnects. See backoff.ts. */
	reconnectBackoff?: BackoffOptions;
}

export interface TaskSync {
	taskId: string;
	description: string;
	status: string;
	projectId: string;
	subtasks: Array<{
		id: string;
		description: string;
		status: string;
		dependsOn: string[];
		assignedWorker?: string;
		result?: string;
		steps?: Array<{ agent: string; prompt: string }>;
	}>;
}

/** Worker status from ListWorkers RPC (mirrors WorkerProto in engine.proto). */
export interface WorkerInfo {
	id: string;
	capabilities: string[];
	currentLoad: number;
	maxCapacity: number;
	loadPercent: number;
	lastHeartbeat: string;
	heartbeatAgeSeconds: number;
	heartbeatStale: boolean;
	isAvailable: boolean;
}

/** Result of listWorkers() — full cluster snapshot with availability flag. */
export interface WorkerListResult {
	available: boolean;
	workers: WorkerInfo[];
	total: number;
	availableCount: number;
	/** True when data comes from Health RPC fallback (approximate, no load/capacity details). */
	degraded?: boolean;
}

/** Structured error from GrpcBridge operations. */
export type BridgeError =
	| { kind: "server_unavailable"; message: string }
	| { kind: "worker_failed"; message: string }
	| { kind: "submit_rejected"; message: string };

/** Result of submitTask — either success or structured error. */
export type SubmitResult = { ok: true; task: TaskSync } | { ok: false; error: BridgeError };

// ── Bridge ─────────────────────────────────────────────────────────

export class GrpcBridge {
	private config: BridgeConfig;
	private connected = false;
	private transport: ReturnType<typeof createGrpcWebTransport>;
	private engineClient: ReturnType<typeof createClient<typeof EngineService>>;
	private taskClient: ReturnType<typeof createClient<typeof TaskService>>;
	private dashboardClient: ReturnType<typeof createClient<typeof DashboardService>>;
	private workerClient: ReturnType<typeof createClient<typeof WorkerService>>;
	/** Monotonic counter bumped by run-omp.sh restart marker. */
	private lastRestartMarker = 0;
	/** Guard: only one reconnect attempt at a time. */
	private reconnecting = false;

	constructor(config?: Partial<BridgeConfig>) {
		this.config = {
			serverUrl: "http://localhost:50051",
			timeoutMs: 10_000,
			...config,
		};

		this.transport = this.createTransport();
		this.engineClient = createClient(EngineService, this.transport);
		this.taskClient = createClient(TaskService, this.transport);
		this.dashboardClient = createClient(DashboardService, this.transport);
		this.workerClient = createClient(WorkerService, this.transport);
	}

	// ── Transport lifecycle ────────────────────────────────────

	private createTransport(): ReturnType<typeof createGrpcWebTransport> {
		return createGrpcWebTransport({ baseUrl: this.config.serverUrl });
	}

	/** Recreate the transport and all service clients. */
	reconnect(): void {
		this.transport = this.createTransport();
		this.engineClient = createClient(EngineService, this.transport);
		this.taskClient = createClient(TaskService, this.transport);
		this.dashboardClient = createClient(DashboardService, this.transport);
		this.workerClient = createClient(WorkerService, this.transport);
		this.connected = false;
	}

	/** Close the bridge — stamp connected false so callers know it's dead. */
	close(): void {
		this.connected = false;
	}

	/** Set or replace the onConnectionChange callback. Used by UCOrchestrator to wire events. */
	setOnConnectionChange(callback: (connected: boolean) => void): void {
		this.config.onConnectionChange = callback;
	}

	/** Check if an error looks like a broken connection (transport-level, not a gRPC business error). */
	static isConnectionError(err: unknown): boolean {
		// Walk the cause chain — connectrpc wraps the real transport error
		// (e.g. ConnectionRefused) inside a ConnectError whose own message is a
		// generic "Unable to connect...". The discriminator lives on the cause.
		const msgs: string[] = [];
		let cur: unknown = err;
		for (let depth = 0; cur instanceof Error && depth < 5; depth++) {
			msgs.push(cur.message.toLowerCase());
			// Bun/Node DOMException-style errors carry a `code` (e.g. "ConnectionRefused").
			const code = (cur as { code?: unknown }).code;
			if (typeof code === "string") msgs.push(code.toLowerCase());
			cur = (cur as { cause?: unknown }).cause;
		}
		const hay = msgs.join(" \n ");
		if (msgs.length === 0) return false;
		return (
			hay.includes("econnrefused") ||
			hay.includes("connectionrefused") ||
			hay.includes("econnreset") ||
			hay.includes("epipe") ||
			hay.includes("enetunreach") ||
			hay.includes("ehostunreach") ||
			hay.includes("unable to connect") ||
			hay.includes("failed to fetch") ||
			hay.includes("network error") ||
			hay.includes("transport not connected") ||
			hay.includes("transport closed") ||
			hay.includes("goaway") ||
			hay.includes("refused stream") ||
			hay.includes("internal http2") ||
			hay.includes("stream error") ||
			hay.includes("connection reset")
		);
	}

	/**
	 * Attempt to reconnect with exponential backoff across multiple attempts.
	 *
	 * Connection errors only (caller / isConnectionError guard). Holds the
	 * `reconnecting` mutex so concurrent callers don't each drive their own
	 * backoff sequence — the first caller does the reconnect, others wait on
	 * the mutex and then re-check connectivity.
	 *
	 * Emits onConnectionChange(false) at the start of the sequence and
	 * onConnectionChange(true) on success.
	 *
	 * Returns true if the bridge is connected after this call (either this
	 * caller reconnected, or a concurrent caller already did).
	 */
	private async tryReconnect(err: unknown): Promise<boolean> {
		if (!GrpcBridge.isConnectionError(err)) return false;
		// If another caller is already mid-reconnect, wait for it then re-check.
		if (this.reconnecting) {
			// Spin-wait on the mutex with a microtask yield. The reconnecting
			// caller either succeeds (connected=true) or exhausts backoff
			// (connected stays false). We don't drive our own sequence.
			while (this.reconnecting) {
				await new Promise<void>((r) => setTimeout(r, 50));
			}
			return this.connected;
		}
		this.reconnecting = true;
		this.connected = false;
		this.config.onConnectionChange?.(false);
		const backoff = this.config.reconnectBackoff ?? {};
		try {
			for (let attempt = 0; ; attempt++) {
				this.reconnect();
				try {
					await this.engineClient.health(create(HealthRequestSchema));
					this.connected = true;
					this.config.onConnectionChange?.(true);
					return true;
				} catch (verifyErr) {
					console.warn(
						`GrpcBridge reconnect attempt ${attempt + 1} failed: ${verifyErr instanceof Error ? verifyErr.message : verifyErr}`,
					);
				}
				const slept = await sleepBackoff(attempt, backoff);
				if (!slept) return false; // exhausted maxAttempts
			}
		} finally {
			this.reconnecting = false;
		}
	}

	/** Check the run-omp.sh restart marker and reconnect if the server restarted. */
	async checkRestartMarker(): Promise<void> {
		try {
			const raw = readFileSync("/tmp/uc-grpc-restart-marker", "utf-8").trim();
			const ts = parseInt(raw, 10);
			if (!isNaN(ts) && ts > this.lastRestartMarker) {
				this.lastRestartMarker = ts;
				this.reconnect();
			}
		} catch (err) {
			// Marker file doesn't exist — that's expected; log only if it's not ENOENT
			if (!(err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT")) {
				console.warn(`GrpcBridge checkRestartMarker failed: ${err instanceof Error ? err.message : err}`);
			}
		}
	}

	/**
	 * Run an RPC call with automatic reconnect-on-connection-error.
	 * On connection error, drives an exponential-backoff reconnect sequence
	 * (see tryReconnect) then retries the operation once. Falls back to
	 * `fallback` if the reconnect sequence exhausts its attempts, the retry
	 * fails, or the error is not connection-related (business errors fail fast).
	 */
	private async withReconnect<T>(fn: () => Promise<T>, fallback: T): Promise<T> {
		try {
			return await fn();
		} catch (err) {
			// Only treat transport-level errors as a connection drop. A business
			// error (NotFound, InvalidArgument, engine rejection) means the server
			// is reachable — flipping connected=false here would falsely signal
			// "UC: disconnected" to the UI on every rejected request.
			if (GrpcBridge.isConnectionError(err)) {
				this.connected = false;
				this.config.onConnectionChange?.(false);
				if (await this.tryReconnect(err)) {
					try {
						return await fn();
					} catch (retryErr) {
						console.warn("GrpcBridge retry after reconnect failed");
					}
				}
			} else {
				// Non-connection error (business error, serialization issue, etc.)
				// — log it so "Indexing failed" reports are debuggable.
				console.warn(`GrpcBridge withReconnect non-connection error:`, err instanceof Error ? `${err.name}: ${err.message}` : err);
			}
			return fallback;
		}
	}

	// ── Health Check ────────────────────────────────────────────

	async health(): Promise<{ status: string; version: string }> {
		await this.checkRestartMarker();
		try {
			const resp = await this.engineClient.health(create(HealthRequestSchema));
			this.connected = true;
			return { status: resp.status, version: resp.version };
		} catch (err) {
			this.connected = false;
			// Reconnect with exponential backoff on connection errors
			if (await this.tryReconnect(err)) {
				try {
					const resp = await this.engineClient.health(create(HealthRequestSchema));
					this.connected = true;
					return { status: resp.status, version: resp.version };
				} catch {
					console.warn("GrpcBridge health check after reconnect failed");
				}
			}
			return { status: "unavailable", version: "0.0.0" };
		}
	}

	// ── Task Operations ────────────────────────────────────────

	async submitTask(description: string, projectId = ""): Promise<SubmitResult> {
		const doSubmit = async (): Promise<SubmitResult> => {
			const resp = await this.taskClient.submitTask(
				create(SubmitTaskRequestSchema, { description, projectId }),
			);
			if (!resp.success) {
				const errMsg = resp.error ?? "unknown reason";
				const kind = errMsg.toLowerCase().includes("worker")
					? "worker_failed"
					: "submit_rejected";
				return { ok: false, error: { kind, message: errMsg } };
			}
			return { ok: true, task: this.parseTaskProto(resp) };
		};
		try {
			return await doSubmit();
		} catch (err) {
			// Only connection errors mean the server is down; business errors
			// (e.g. malformed request) leave the connection intact.
			if (GrpcBridge.isConnectionError(err)) {
				this.connected = false;
				if (await this.tryReconnect(err)) {
					try { return await doSubmit(); } catch { console.warn("GrpcBridge submitTask retry failed"); }
				}
			}
			const msg = err instanceof Error ? err.message : String(err);
			const isConn = GrpcBridge.isConnectionError(err);
			return {
				ok: false,
				error: {
					kind: isConn ? "server_unavailable" : "submit_rejected",
					message: isConn
						? "gRPC server unavailable — start with ./run-omp.sh"
						: msg,
				},
			};
		}
	}

	async getTask(taskId: string): Promise<TaskSync | null> {
		return this.withReconnect(async () => {
			const resp = await this.taskClient.getTask(
				create(GetTaskRequestSchema, { taskId }),
			);
			if (!resp.available) return null;
			return resp.task ? this.parseTaskFromProto(resp.task) : null;
		}, null);
	}

	async listTasks(): Promise<TaskSync[]> {
		return this.withReconnect(async () => {
			const resp = await this.taskClient.listTasks(create(ListTasksRequestSchema));
			if (!resp.available) return [];
			return resp.tasks.map((t) => this.parseTaskFromProto(t));
		}, []);
	}

	// ── WatchTask Streaming ─────────────────────────────────────

	/**
	 * Start a WatchTask server-streaming RPC (empty taskId = watch all tasks).
	 * Returns an AbortController the caller uses to stop the stream.
	 *
	 * On normal stream end (server closed) or error (when not caller-aborted),
	 * the optional onError callback is invoked so the caller can reconnect.
	 *
	 * Pattern mirrors dashboard/src/hooks/useGrpcWeb.ts:210-235.
	 */
	startWatchTask(
		onEvent: (ev: TaskEvent) => void,
		onError?: (err: unknown) => void,
	): AbortController {
		const ac = new AbortController();
		const req = create(WatchTaskRequestSchema, { taskId: "" });
		// async IIFE — fire-and-forget; caller manages lifecycle via the AbortController
		void (async () => {
			try {
				const stream = this.taskClient.watchTask(req, { signal: ac.signal });
				for await (const event of stream) {
					if (ac.signal.aborted) break;
					try {
						onEvent(event);
					} catch (cbErr) {
						console.warn(`GrpcBridge watchTask onEvent callback error: ${cbErr instanceof Error ? cbErr.message : cbErr}`);
					}
				}
				// Stream ended normally (not aborted) — server closed it
				if (!ac.signal.aborted) {
					console.warn("GrpcBridge watchTask stream ended (server closed)");
					onError?.(new Error("watchTask stream closed by server"));
				}
			} catch (err) {
				if (ac.signal.aborted) return; // caller-initiated stop
				console.warn(`GrpcBridge watchTask stream error: ${err instanceof Error ? err.message : err}`);
				onError?.(err);
			}
		})();
		return ac;
	}

	// ── Upsert (create or update) ──────────────────────────────

	async upsertTask(task: import("./task-store").PersistedTask): Promise<boolean> {
		return this.withReconnect(async () => {
			// UpdateTaskRequest now carries description + projectId for
			// create-if-not-exists on the server. No more getTask+submitTask
			// dance — the server preserves the orchestrator's original task ID.
			const resp = await this.taskClient.updateTask(
				create(UpdateTaskRequestSchema, {
					taskId: task.id,
					status: task.status,
					description: task.description,
					projectId: task.projectId ?? "",
					subtasks: task.subtasks.map((st) => ({
						id: st.id,
						description: st.description,
						status: st.status,
						dependsOn: st.dependsOn,
						result: st.result ?? "",
						parentId: task.id,
						expectedOutput: "",
						fileConstraints: st.files ?? [],
						dispatchMode: st.dispatchMode,
						requiredCapabilities: st.requiredCapabilities ?? [],
						// ponytail: map SubtaskDef.steps (snake_case) → proto camelCase fields
						steps: (st.steps ?? []).map((s) => ({
							agent: s.agent,
							prompt: s.prompt,
							...(s.agent_config_json != null ? { agentConfigJson: s.agent_config_json } : {}),
							...(s.abort_on_failure != null ? { abortOnFailure: s.abort_on_failure } : {}),
						})),
					})),
				}),
			);
			return resp.success;
		}, false);
	}

	// ── Task Control ────────────────────────────────────────────

	async pauseTask(taskId: string): Promise<boolean> {
		return this.withReconnect(async () => {
			const resp = await this.taskClient.pauseTask(
				create(PauseTaskRequestSchema, { taskId }),
			);
			return resp.success;
		}, false);
	}

	async resumeTask(taskId: string): Promise<boolean> {
		return this.withReconnect(async () => {
			const resp = await this.taskClient.resumeTask(
				create(ResumeTaskRequestSchema, { taskId }),
			);
			return resp.success;
		}, false);
	}

	async cancelTask(taskId: string, _subtaskId?: string): Promise<boolean> {
		// ponytail: CancelTaskRequest has no subtaskId field — server ignores it
		return this.withReconnect(async () => {
			const resp = await this.taskClient.cancelTask(
				create(CancelTaskRequestSchema, { taskId }),
			);
			return resp.success;
		}, false);
	}

	// ── Memory Operations ──────────────────────────────────────

	async readMemory(
		keyScope: string,
		key: string,
		taskId = "",
		projectId = "",
	): Promise<string | null> {
		return this.withReconnect(async () => {
			const resp = await this.engineClient.readMemory(
				create(ReadMemoryRequestSchema, { keyScope, key, taskId, projectId }),
			);
			return resp.entry?.content ?? null;
		}, null);
	}

	async writeMemory(
		keyScope: string,
		key: string,
		content: string,
		contentType = "text",
		sourceAgent = "uc-orchestrator",
		taskId = "",
		projectId = "",
		importance?: number,
		tags?: string[],
	): Promise<boolean> {
		return this.withReconnect(async () => {
			await this.engineClient.writeMemory(
				create(WriteMemoryRequestSchema, {
					keyScope, key, content, contentType, sourceAgent, taskId, projectId,
					importance: importance ?? 0,
					tags: tags ?? [],
				}),
			);
			return true;
		}, false);
	}

	async searchMemory(
		query: string,
		scopeType = "all",
		projectId = "",
		maxResults = 5,
	): Promise<Array<{ content: string; score: number }>> {
		return this.withReconnect(async () => {
			const resp = await this.engineClient.searchMemory(
				create(SearchMemoryRequestSchema, { query, scopeType, projectId, maxResults }),
			);
			return resp.results.map((r) => ({
				content: r.entry?.content ?? "",
				score: r.score,
			}));
		}, []);
	}

	async deleteMemory(
		keyScope: string,
		key: string,
		taskId = "",
		projectId = "",
	): Promise<boolean> {
		return this.withReconnect(async () => {
			// ponytail: DeleteMemoryResponse is empty — success = no error
			await this.engineClient.deleteMemory(
				create(DeleteMemoryRequestSchema, { keyScope, key, taskId, projectId }),
			);
			return true;
		}, false);
	}

	async batchWriteMemory(
		entries: Array<{ keyScope: string; key: string; content: string; contentType?: string }>,
		sourceAgent = "uc-orchestrator",
	): Promise<number> {
		return this.withReconnect(async () => {
			// ponytail: BatchWriteMemoryRequest.requests is WriteMemoryRequest[]
			const resp = await this.engineClient.batchWriteMemory(
				create(BatchWriteMemoryRequestSchema, {
					requests: entries.map((e) => ({
						keyScope: e.keyScope,
						key: e.key,
						content: e.content,
						contentType: e.contentType ?? "text",
						sourceAgent,
						taskId: "",
						projectId: "",
						importance: 0,
						tags: [],
					})),
				}),
			);
			return resp.entries.length;
		}, 0);
	}

	// ── Search Operations ──────────────────────────────────────

	async searchCode(
		query: string,
		modes: string[] = ["hybrid"],
		maxResults = 10,
		repoIds?: string[],
		languages?: string[],
		pathPatterns?: string[],
	): Promise<Array<{ repoId: string; filePath: string; snippet: string; score: number }>> {
		return this.withReconnect(async () => {
			const resp = await this.engineClient.search(
				create(SearchRequestSchema, {
					query, modes, maxResults,
					repoIds: repoIds ?? [],
					languages: languages ?? [],
					pathPatterns: pathPatterns ?? [],
				}),
			);
			return resp.items.map((item) => ({
				repoId: item.repoId,
				filePath: item.filePath,
				snippet: item.contentSnippet,
				score: item.score,
			}));
		}, []);
	}

	// ── Index Operations ──────────────────────────────────────

	async indexRepo(
		repoId: string,
		localPath: string,
		_languages: string[] = [],
		workspaceId: string = "default",
	): Promise<boolean> {
		return this.withReconnect(async () => {
			// ponytail: IndexRepoRequest has no languages field; IndexRepoResponse has no success
			await this.engineClient.indexRepo(
				create(IndexRepoRequestSchema, { repoId, localPath, workspaceId }),
			);
			return true;
		}, false);
	}

	async getIndexState(repoId: string): Promise<{ status: string; indexedFiles: number; lastIndexed: string } | null> {
		return this.withReconnect(async () => {
			const resp = await this.engineClient.getIndexState(
				create(GetIndexStateRequestSchema, { repoId }),
			);
			if (!resp.indexed) return null;
			return {
				status: "indexed",
				indexedFiles: resp.filesCount,
				lastIndexed: resp.lastIndexedSha ?? "",
			};
		}, null);
	}

	async removeIndex(repoId: string): Promise<boolean> {
		return this.withReconnect(async () => {
			// ponytail: RemoveIndexResponse is empty — success = no error
			await this.engineClient.removeIndex(
				create(RemoveIndexRequestSchema, { repoId }),
			);
			return true;
		}, false);
	}

	async listRepos(workspaceId?: string): Promise<Array<{ repoId: string; workspaceId: string; status: string; indexedFiles: number }>> {
		return this.withReconnect(async () => {
			const resp = await this.engineClient.listRepos(
				create(ListReposRequestSchema, workspaceId ? { workspaceId } : {}),
			);
			return resp.repos.map((r) => ({
				repoId: r.repoId,
				workspaceId: r.workspaceId,
				status: r.indexed ? "indexed" : "unknown",
				indexedFiles: r.filesCount,
			}));
		}, []);
	}

	// ── File Operations ───────────────────────────────────────

	async listDir(
		path: string,
		repoId?: string,
	): Promise<Array<{ name: string; type: string; size: number }>> {
		return this.withReconnect(async () => {
			const resp = await this.engineClient.listDir(
				create(ListDirRequestSchema, { path, repoId: repoId ?? "" }),
			);
			return resp.entries.map((e) => ({
				name: e.name,
				type: e.entryType,
				size: Number(e.size),
			}));
		}, []);
	}

	async getFile(path: string, repoId?: string): Promise<string | null> {
		return this.withReconnect(async () => {
			const resp = await this.engineClient.getFile(
				create(GetFileRequestSchema, { path, repoId: repoId ?? "" }),
			);
			return resp.content ?? null;
		}, null);
	}

	// ── Worker Operations ──────────────────────────────────────

	async listWorkers(): Promise<WorkerListResult> {
		return this.withReconnect(async () => {
			const resp = await this.dashboardClient.listWorkers(
				create(ListWorkersRequestSchema),
			);
			this.connected = true;
			const workers: WorkerInfo[] = resp.workers.map((w) => ({
				id: w.id,
				capabilities: [...w.capabilities],
				currentLoad: w.currentLoad,
				maxCapacity: w.maxCapacity,
				loadPercent: w.loadPercent,
				lastHeartbeat: w.lastHeartbeat,
				heartbeatAgeSeconds: w.heartbeatAgeSeconds,
				heartbeatStale: w.heartbeatStale,
				isAvailable: w.isAvailable,
			}));
			return {
				available: resp.available,
				workers,
				total: resp.total,
				availableCount: resp.availableCount,
			};
		}, { available: false, workers: [], total: 0, availableCount: 0 });
	}

	/**
	 * Scale the worker cluster or force-deregister a worker via WorkerService.ScaleWorkers.
	 *
	 * - action="scale": gateway shell-outs `docker compose up -d --no-deps --scale worker=<target> worker`.
	 *   New workers self-register; scaled-down workers self-deregister on SIGTERM.
	 * - action="deregister": force-removes a worker_id from the in-memory WorkerRegistry
	 *   WITHOUT stopping its process. Use to evict stale/ghost workers.
	 *
	 * On transport unavailable, returns a failure result (consistent with listWorkers).
	 */
	async scaleWorkers(
		action: "scale" | "deregister",
		opts: { targetCount?: number; workerId?: string },
	): Promise<{ success: boolean; error?: string; actualCount: number; message: string }> {
		return this.withReconnect(async () => {
			const resp = await this.workerClient.scaleWorkers(
				create(ScaleWorkersRequestSchema, {
					action,
					targetCount: opts.targetCount ?? 0,
					workerId: opts.workerId ?? "",
				}),
			);
			this.connected = true;
			return {
				success: resp.success,
				error: resp.error ?? undefined,
				actualCount: resp.actualCount,
				message: resp.message,
			};
		}, {
			success: false,
			error: "gRPC server unavailable — start with ./run-omp.sh",
			actualCount: 0,
			message: "worker service unavailable",
		});
	}

	// ── Connection ─────────────────────────────────────────────

	isConnected(): boolean {
		return this.connected;
	}

	// ── Internal ───────────────────────────────────────────────

	private parseTaskFromProto(task: { id: string; description: string; status: string; projectId: string; subtasks: Array<{ id: string; description: string; status: string; dependsOn: string[]; assignedWorker?: string; result?: string; steps?: Array<{ agent: string; prompt: string }> }> }): TaskSync {
		return {
			taskId: task.id,
			description: task.description,
			status: task.status,
			projectId: task.projectId,
			subtasks: task.subtasks.map((st) => ({
				id: st.id,
				description: st.description,
				status: st.status,
				dependsOn: [...st.dependsOn],
				assignedWorker: st.assignedWorker,
				result: st.result,
				steps: st.steps?.map((s) => ({ agent: s.agent, prompt: s.prompt })),
			})),
		};
	}

	private parseTaskProto(resp: SubmitTaskResponse): TaskSync {
		// ponytail: SubmitTaskResponse has inline task fields (id, status, subtasks)
		return {
			taskId: resp.taskId,
			description: "",
			status: resp.status,
			projectId: "",
			subtasks: resp.subtasks.map((st) => ({
				id: st.id,
				description: st.description,
				status: st.status,
				dependsOn: [...st.dependsOn],
				assignedWorker: st.assignedWorker,
				result: st.result,
				steps: st.steps?.map((s) => ({ agent: s.agent, prompt: s.prompt })),
			})),
		};
	}
}
