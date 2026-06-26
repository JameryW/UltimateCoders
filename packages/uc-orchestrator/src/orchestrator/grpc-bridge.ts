/**
 * gRPC Bridge — Connects uc-orchestrator to UC Rust core engine.
 *
 * Uses gRPC-Web (HTTP+JSON) to communicate with the UC gRPC server.
 * This avoids needing native gRPC libraries in the Bun runtime.
 *
 * ponytail: HTTP fetch + JSON — no @grpc/grpc-js dependency.
 * Upgrade to native gRPC if perf matters.
 */

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

// ponytail: internal response type to avoid Record<string, unknown> everywhere
type RpcResp = Record<string, unknown>;

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

	constructor(config?: Partial<BridgeConfig>) {
		this.config = {
			serverUrl: "http://localhost:50051",
			timeoutMs: 10_000,
			...config,
		};
	}

	// ── Health Check ────────────────────────────────────────────

	async health(): Promise<{ status: string; version: string }> {
		try {
			const resp = await this.rpc("Health", {});
			return {
				status: (resp.status as string) ?? "unknown",
				version: (resp.version as string) ?? "0.0.0",
			};
		} catch {
			return { status: "unavailable", version: "0.0.0" };
		}
	}

	// ── Task Operations ────────────────────────────────────────

	async submitTask(description: string, projectId = ""): Promise<SubmitResult> {
		try {
			const resp = await this.rpc("SubmitTask", {
				description,
				project_id: projectId,
			});
			if (!(resp.success as boolean)) {
				const errMsg = (resp.error as string) ?? "unknown reason";
				// Distinguish worker failure from generic rejection
				const kind = errMsg.toLowerCase().includes("worker")
					? "worker_failed"
					: "submit_rejected";
				return { ok: false, error: { kind, message: errMsg } };
			}
			return { ok: true, task: this.parseTaskSync(resp) };
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			// Connection refused / network error → server unavailable
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
			const resp = await this.rpc("GetTask", { task_id: taskId });
			if (!(resp.available as boolean)) return null;
			return this.parseTaskSync(resp.task as RpcResp);
		} catch {
			return null;
		}
	}

	async listTasks(): Promise<TaskSync[]> {
		try {
			const resp = await this.rpc("ListTasks", {});
			if (!(resp.available as boolean)) return [];
			return ((resp.tasks as RpcResp[]) ?? []).map(this.parseTaskSync);
		} catch {
			return [];
		}
	}

	// ── Upsert (create or update) ──────────────────────────────

	/**
	 * Upsert task: if task exists on server, call UpdateTask; otherwise SubmitTask.
	 */
	async upsertTask(task: import("./task-store").PersistedTask): Promise<boolean> {
		try {
			const existing = await this.getTask(task.id);
			if (existing) {
				// Task exists — update via UpdateTask RPC
				const resp = await this.rpc("UpdateTask", {
					task_id: task.id,
					status: task.status,
					subtasks: task.subtasks.map((st) => ({
						id: st.id,
						description: st.description,
						status: st.status,
						depends_on: st.dependsOn,
						result: st.result ?? "",
					})),
				});
				return (resp.success as boolean) ?? false;
			}

			const resp = await this.rpc("SubmitTask", {
				description: task.description,
				project_id: "",
				task_id: task.id,
				status: task.status,
				control_state: task.controlState,
				subtasks: task.subtasks.map((st) => ({
					id: st.id,
					description: st.description,
					status: st.status,
					depends_on: st.dependsOn,
					result: st.result ?? "",
					error: st.error ?? "",
				})),
			});
			return (resp.success as boolean) ?? false;
		} catch {
			return false;
		}
	}

	// ── Task Control ────────────────────────────────────────────

	async pauseTask(taskId: string): Promise<boolean> {
		try {
			const resp = await this.rpc("PauseTask", { task_id: taskId });
			return (resp.success as boolean) ?? false;
		} catch {
			return false;
		}
	}

	async resumeTask(taskId: string): Promise<boolean> {
		try {
			const resp = await this.rpc("ResumeTask", { task_id: taskId });
			return (resp.success as boolean) ?? false;
		} catch {
			return false;
		}
	}

	async cancelTask(taskId: string, subtaskId?: string): Promise<boolean> {
		try {
			const resp = await this.rpc("CancelTask", {
				task_id: taskId,
				subtask_id: subtaskId ?? "",
			});
			return (resp.success as boolean) ?? false;
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
			const resp = await this.rpc("ReadMemory", {
				key_scope: keyScope,
				key,
				task_id: taskId,
				project_id: projectId,
			});
			const entry = resp.entry as RpcResp | undefined;
			return entry?.content as string ?? null;
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
			const payload: Record<string, unknown> = {
				key_scope: keyScope,
				key,
				content,
				content_type: contentType,
				source_agent: sourceAgent,
				task_id: taskId,
				project_id: projectId,
			};
			if (importance !== undefined) payload.importance = importance;
			if (tags?.length) payload.tags = tags;
			await this.rpc("WriteMemory", payload);
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
			const resp = await this.rpc("SearchMemory", {
				query,
				scope_type: scopeType,
				project_id: projectId,
				max_results: maxResults,
			});
			return ((resp.results as RpcResp[]) ?? []).map((r) => ({
				content: ((r.entry as RpcResp | undefined)?.content as string) ?? "",
				score: (r.score as number) ?? 0,
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
			const resp = await this.rpc("DeleteMemory", {
				key_scope: keyScope,
				key,
				task_id: taskId,
				project_id: projectId,
			});
			return (resp.success as boolean) ?? false;
		} catch {
			return false;
		}
	}

	async batchWriteMemory(
		entries: Array<{ keyScope: string; key: string; content: string; contentType?: string }>,
		sourceAgent = "uc-orchestrator",
	): Promise<number> {
		try {
			const resp = await this.rpc("BatchWriteMemory", {
				entries: entries.map((e) => ({
					key_scope: e.keyScope,
					key: e.key,
					content: e.content,
					content_type: e.contentType ?? "text",
					source_agent: sourceAgent,
				})),
			});
			return (resp.count as number) ?? 0;
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
			const payload: Record<string, unknown> = {
				query,
				modes,
				max_results: maxResults,
			};
			if (repoIds?.length) payload.repo_ids = repoIds;
			if (languages?.length) payload.languages = languages;
			if (pathPatterns?.length) payload.path_patterns = pathPatterns;
			const resp = await this.rpc("Search", payload);
			return ((resp.items as RpcResp[]) ?? []).map((item) => ({
				filePath: (item.file_path as string) ?? "",
				snippet: (item.content_snippet as string) ?? "",
				score: (item.score as number) ?? 0,
			}));
		} catch {
			return [];
		}
	}

	// ── Index Operations ──────────────────────────────────────

	async indexRepo(
		repoId: string,
		localPath: string,
		languages: string[] = [],
	): Promise<boolean> {
		try {
			const resp = await this.rpc("IndexRepo", {
				repo_id: repoId,
				local_path: localPath,
				languages,
			});
			return (resp.success as boolean) ?? false;
		} catch {
			return false;
		}
	}

	async getIndexState(repoId: string): Promise<{ status: string; indexedFiles: number; lastIndexed: string } | null> {
		try {
			const resp = await this.rpc("GetIndexState", { repo_id: repoId });
			if (!(resp.available as boolean)) return null;
			return {
				status: (resp.status as string) ?? "unknown",
				indexedFiles: (resp.indexed_files as number) ?? 0,
				lastIndexed: (resp.last_indexed as string) ?? "",
			};
		} catch {
			return null;
		}
	}

	async removeIndex(repoId: string): Promise<boolean> {
		try {
			const resp = await this.rpc("RemoveIndex", { repo_id: repoId });
			return (resp.success as boolean) ?? false;
		} catch {
			return false;
		}
	}

	async listRepos(): Promise<Array<{ repoId: string; status: string; indexedFiles: number }>> {
		try {
			const resp = await this.rpc("ListRepos", {});
			return ((resp.repos as RpcResp[]) ?? []).map((r) => ({
				repoId: (r.repo_id as string) ?? "",
				status: (r.status as string) ?? "",
				indexedFiles: (r.indexed_files as number) ?? 0,
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
			const payload: Record<string, unknown> = { path };
			if (repoId) payload.repo_id = repoId;
			const resp = await this.rpc("ListDir", payload);
			return ((resp.entries as RpcResp[]) ?? []).map((e) => ({
				name: (e.name as string) ?? "",
				type: (e.type as string) ?? "file",
				size: (e.size as number) ?? 0,
			}));
		} catch {
			return [];
		}
	}

	async getFile(path: string, repoId?: string): Promise<string | null> {
		try {
			const payload: Record<string, unknown> = { path };
			if (repoId) payload.repo_id = repoId;
			const resp = await this.rpc("GetFile", payload);
			return (resp.content as string) ?? null;
		} catch {
			return null;
		}
	}

	// ── Worker Operations ──────────────────────────────────────

	async listWorkers(): Promise<WorkerListResult> {
		try {
			const resp = await this.rpc("ListWorkers", {});
			const workers = ((resp.workers as RpcResp[]) ?? []).map((w): WorkerInfo => ({
				id: (w.id as string) ?? "",
				capabilities: (w.capabilities as string[]) ?? [],
				currentLoad: (w.current_load as number) ?? 0,
				maxCapacity: (w.max_capacity as number) ?? 0,
				loadPercent: (w.load_percent as number) ?? 0,
				lastHeartbeat: (w.last_heartbeat as string) ?? "",
				heartbeatAgeSeconds: (w.heartbeat_age_seconds as number) ?? 0,
				heartbeatStale: (w.heartbeat_stale as boolean) ?? false,
				isAvailable: (w.is_available as boolean) ?? false,
			}));
			return {
				available: (resp.available as boolean) ?? false,
				workers,
				total: (resp.total as number) ?? 0,
				availableCount: (resp.available_count as number) ?? 0,
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

	private async rpc(method: string, payload: Record<string, unknown>): Promise<RpcResp> {
		const service = this.resolveService(method);
		const url = `${this.config.serverUrl}/ultimate_coders.${service}/${method}`;

		const controller = new AbortController();
		const timer = setTimeout(() => controller.abort(), this.config.timeoutMs);

		try {
			const resp = await fetch(url, {
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					Accept: "application/json",
				},
				body: JSON.stringify(payload),
				signal: controller.signal,
			});

			if (!resp.ok) {
				throw new Error(`gRPC ${method} failed: ${resp.status}`);
			}

			this.connected = true;
			return (await resp.json()) as RpcResp;
		} finally {
			clearTimeout(timer);
		}
	}

	private resolveService(method: string): string {
		const taskMethods = new Set([
			"SubmitTask", "GetTask", "ListTasks",
			"WatchTask", "PauseTask", "ResumeTask", "CancelTask", "UpdateTask",
		]);
		const engineMethods = new Set([
			"Search", "IndexRepo", "GetIndexState", "RemoveIndex",
			"ReadMemory", "WriteMemory", "DeleteMemory", "SearchMemory",
			"Health", "BatchWriteMemory", "ListRepos", "ListDir", "GetFile",
		]);
		const dashboardMethods = new Set([
			"ListWorkers", "GetSchedulerStatus", "GetDashboardData",
		]);

		if (taskMethods.has(method)) return "TaskService";
		if (engineMethods.has(method)) return "EngineService";
		if (dashboardMethods.has(method)) return "DashboardService";
		return "EngineService";
	}

	private parseTaskSync = (raw: RpcResp): TaskSync => ({
		taskId: (raw.id as string) ?? "",
		description: (raw.description as string) ?? "",
		status: (raw.status as string) ?? "",
		projectId: (raw.project_id as string) ?? "",
		subtasks: ((raw.subtasks as RpcResp[]) ?? []).map((st) => ({
			id: (st.id as string) ?? "",
			description: (st.description as string) ?? "",
			status: (st.status as string) ?? "",
			dependsOn: (st.depends_on as string[]) ?? [],
			assignedWorker: st.assigned_worker as string | undefined,
			result: st.result as string | undefined,
		})),
	});
}
