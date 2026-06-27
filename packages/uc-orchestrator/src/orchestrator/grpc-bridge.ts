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
	type SubmitTaskResponse,
} from "../grpc/engine_pb.js";
import { create } from "@bufbuild/protobuf";

// ── Types ──────────────────────────────────────────────────────────

export interface BridgeConfig {
	/** gRPC server address. Default: "http://localhost:50051" */
	serverUrl: string;
	/** Request timeout (ms). Default: 10000 */
	timeoutMs: number;
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

	constructor(config?: Partial<BridgeConfig>) {
		this.config = {
			serverUrl: "http://localhost:50051",
			timeoutMs: 10_000,
			...config,
		};

		// ponytail: single transport, shared across all service clients
		this.transport = createGrpcWebTransport({
			baseUrl: this.config.serverUrl,
		});
		this.engineClient = createClient(EngineService, this.transport);
		this.taskClient = createClient(TaskService, this.transport);
		this.dashboardClient = createClient(DashboardService, this.transport);
	}

	// ── Health Check ────────────────────────────────────────────

	async health(): Promise<{ status: string; version: string }> {
		try {
			const resp = await this.engineClient.health(create(HealthRequestSchema));
			this.connected = true;
			return { status: resp.status, version: resp.version };
		} catch {
			return { status: "unavailable", version: "0.0.0" };
		}
	}

	// ── Task Operations ────────────────────────────────────────

	async submitTask(description: string, projectId = ""): Promise<SubmitResult> {
		try {
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
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			return {
				ok: false,
				error: {
					kind: "server_unavailable",
					message: msg.includes("fetch") || msg.includes("ECONNREFUSED") || msg.includes("Failed to fetch")
						? "gRPC server unavailable — start with ./run-omp.sh"
						: msg,
				},
			};
		}
	}

	async getTask(taskId: string): Promise<TaskSync | null> {
		try {
			const resp = await this.taskClient.getTask(
				create(GetTaskRequestSchema, { taskId }),
			);
			if (!resp.available) return null;
			return resp.task ? this.parseTaskFromProto(resp.task) : null;
		} catch {
			return null;
		}
	}

	async listTasks(): Promise<TaskSync[]> {
		try {
			const resp = await this.taskClient.listTasks(create(ListTasksRequestSchema));
			if (!resp.available) return [];
			return resp.tasks.map((t) => this.parseTaskFromProto(t));
		} catch {
			return [];
		}
	}

	// ── Upsert (create or update) ──────────────────────────────

	async upsertTask(task: import("./task-store").PersistedTask): Promise<boolean> {
		try {
			const existing = await this.getTask(task.id);
			if (existing) {
				// ponytail: SubtaskProto requires parentId + expectedOutput (non-optional strings)
				const resp = await this.taskClient.updateTask(
					create(UpdateTaskRequestSchema, {
						taskId: task.id,
						status: task.status,
						subtasks: task.subtasks.map((st) => ({
							id: st.id,
							description: st.description,
							status: st.status,
							dependsOn: st.dependsOn,
							result: st.result ?? "",
							parentId: task.id,
							expectedOutput: "",
							fileConstraints: [],
						})),
					}),
				);
				return resp.success;
			}

			// ponytail: SubmitTaskRequest only has description + projectId;
			// extra fields (taskId, status, subtasks) were sent via old JSON bridge
			// but silently ignored by the server
			const resp = await this.taskClient.submitTask(
				create(SubmitTaskRequestSchema, {
					description: task.description,
					projectId: "",
				}),
			);
			return resp.success;
		} catch {
			return false;
		}
	}

	// ── Task Control ────────────────────────────────────────────

	async pauseTask(taskId: string): Promise<boolean> {
		try {
			const resp = await this.taskClient.pauseTask(
				create(PauseTaskRequestSchema, { taskId }),
			);
			return resp.success;
		} catch {
			return false;
		}
	}

	async resumeTask(taskId: string): Promise<boolean> {
		try {
			const resp = await this.taskClient.resumeTask(
				create(ResumeTaskRequestSchema, { taskId }),
			);
			return resp.success;
		} catch {
			return false;
		}
	}

	async cancelTask(taskId: string, _subtaskId?: string): Promise<boolean> {
		// ponytail: CancelTaskRequest has no subtaskId field — server ignores it
		try {
			const resp = await this.taskClient.cancelTask(
				create(CancelTaskRequestSchema, { taskId }),
			);
			return resp.success;
		} catch {
			return false;
		}
	}

	// ── Memory Operations ──────────────────────────────────────

	async readMemory(
		keyScope: string,
		key: string,
		taskId = "",
		projectId = "",
	): Promise<string | null> {
		try {
			const resp = await this.engineClient.readMemory(
				create(ReadMemoryRequestSchema, { keyScope, key, taskId, projectId }),
			);
			return resp.entry?.content ?? null;
		} catch {
			return null;
		}
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
		try {
			await this.engineClient.writeMemory(
				create(WriteMemoryRequestSchema, {
					keyScope, key, content, contentType, sourceAgent, taskId, projectId,
					importance: importance ?? 0,
					tags: tags ?? [],
				}),
			);
			return true;
		} catch {
			return false;
		}
	}

	async searchMemory(
		query: string,
		scopeType = "all",
		projectId = "",
		maxResults = 5,
	): Promise<Array<{ content: string; score: number }>> {
		try {
			const resp = await this.engineClient.searchMemory(
				create(SearchMemoryRequestSchema, { query, scopeType, projectId, maxResults }),
			);
			return resp.results.map((r) => ({
				content: r.entry?.content ?? "",
				score: r.score,
			}));
		} catch {
			return [];
		}
	}

	async deleteMemory(
		keyScope: string,
		key: string,
		taskId = "",
		projectId = "",
	): Promise<boolean> {
		try {
			// ponytail: DeleteMemoryResponse is empty — success = no error
			await this.engineClient.deleteMemory(
				create(DeleteMemoryRequestSchema, { keyScope, key, taskId, projectId }),
			);
			return true;
		} catch {
			return false;
		}
	}

	async batchWriteMemory(
		entries: Array<{ keyScope: string; key: string; content: string; contentType?: string }>,
		sourceAgent = "uc-orchestrator",
	): Promise<number> {
		try {
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
		} catch {
			return 0;
		}
	}

	// ── Search Operations ──────────────────────────────────────

	async searchCode(
		query: string,
		modes: string[] = ["hybrid"],
		maxResults = 5,
		repoIds?: string[],
		languages?: string[],
		pathPatterns?: string[],
	): Promise<Array<{ filePath: string; snippet: string; score: number }>> {
		try {
			const resp = await this.engineClient.search(
				create(SearchRequestSchema, {
					query, modes, maxResults,
					repoIds: repoIds ?? [],
					languages: languages ?? [],
					pathPatterns: pathPatterns ?? [],
				}),
			);
			return resp.items.map((item) => ({
				filePath: item.filePath,
				snippet: item.contentSnippet,
				score: item.score,
			}));
		} catch {
			return [];
		}
	}

	// ── Index Operations ──────────────────────────────────────

	async indexRepo(
		repoId: string,
		localPath: string,
		_languages: string[] = [],
	): Promise<boolean> {
		try {
			// ponytail: IndexRepoRequest has no languages field; IndexRepoResponse has no success
			await this.engineClient.indexRepo(
				create(IndexRepoRequestSchema, { repoId, localPath }),
			);
			return true;
		} catch {
			return false;
		}
	}

	async getIndexState(repoId: string): Promise<{ status: string; indexedFiles: number; lastIndexed: string } | null> {
		try {
			const resp = await this.engineClient.getIndexState(
				create(GetIndexStateRequestSchema, { repoId }),
			);
			if (!resp.indexed) return null;
			return {
				status: "indexed",
				indexedFiles: resp.filesCount,
				lastIndexed: resp.lastIndexedSha ?? "",
			};
		} catch {
			return null;
		}
	}

	async removeIndex(repoId: string): Promise<boolean> {
		try {
			// ponytail: RemoveIndexResponse is empty — success = no error
			await this.engineClient.removeIndex(
				create(RemoveIndexRequestSchema, { repoId }),
			);
			return true;
		} catch {
			return false;
		}
	}

	async listRepos(): Promise<Array<{ repoId: string; status: string; indexedFiles: number }>> {
		try {
			const resp = await this.engineClient.listRepos(create(ListReposRequestSchema));
			return resp.repos.map((r) => ({
				repoId: r.repoId,
				status: r.indexed ? "indexed" : "unknown",
				indexedFiles: r.filesCount,
			}));
		} catch {
			return [];
		}
	}

	// ── File Operations ───────────────────────────────────────

	async listDir(
		path: string,
		repoId?: string,
	): Promise<Array<{ name: string; type: string; size: number }>> {
		try {
			const resp = await this.engineClient.listDir(
				create(ListDirRequestSchema, { path, repoId: repoId ?? "" }),
			);
			return resp.entries.map((e) => ({
				name: e.name,
				type: e.entryType,
				size: Number(e.size),
			}));
		} catch {
			return [];
		}
	}

	async getFile(path: string, repoId?: string): Promise<string | null> {
		try {
			const resp = await this.engineClient.getFile(
				create(GetFileRequestSchema, { path, repoId: repoId ?? "" }),
			);
			return resp.content ?? null;
		} catch {
			return null;
		}
	}

	// ── Worker Operations ──────────────────────────────────────

	async listWorkers(): Promise<WorkerListResult> {
		try {
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
		} catch {
			// Fallback: try Health RPC for local_worker status
			// ponytail: degraded mode — no load/capacity data from Health RPC,
			// set maxCapacity=-1 to signal "unknown capacity" (0 would imply infinite)
			try {
				const h = await this.health();
				const isHealthy = h.status !== "unavailable";
				return {
					available: true,
					workers: [{
						id: "local_worker",
						capabilities: [],
						currentLoad: 0,
						maxCapacity: -1,
						loadPercent: 0,
						lastHeartbeat: "",
						heartbeatAgeSeconds: 0,
						heartbeatStale: !isHealthy,
						isAvailable: isHealthy,
					}],
					total: 1,
					availableCount: isHealthy ? 1 : 0,
					degraded: true,
				};
			} catch {
				return { available: false, workers: [], total: 0, availableCount: 0 };
			}
		}
	}

	// ── Connection ─────────────────────────────────────────────

	isConnected(): boolean {
		return this.connected;
	}

	// ── Internal ───────────────────────────────────────────────

	private parseTaskFromProto(task: { id: string; description: string; status: string; projectId: string; subtasks: Array<{ id: string; description: string; status: string; dependsOn: string[]; assignedWorker?: string; result?: string }> }): TaskSync {
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
			})),
		};
	}
}
