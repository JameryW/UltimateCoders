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

// ponytail: internal response type to avoid Record<string, unknown> everywhere
type RpcResp = Record<string, unknown>;

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

	async submitTask(description: string, projectId = ""): Promise<TaskSync | null> {
		try {
			const resp = await this.rpc("SubmitTask", {
				description,
				project_id: projectId,
			});
			if (!(resp.success as boolean)) return null;
			return this.parseTaskSync(resp);
		} catch {
			return null;
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
	 * Upsert task: if task exists on server, update it; otherwise create.
	 * ponytail: server has no UpdateTask RPC yet, so we check existence
	 * with GetTask first and skip if already present. Full upsert
	 * requires adding UpdateTask to the proto — tracked as TODO.
	 */
	async upsertTask(task: import("./task-store").PersistedTask): Promise<boolean> {
		try {
			// Check if task already exists on server
			const existing = await this.getTask(task.id);
			if (existing) {
				// Task exists — server has no UpdateTask RPC yet.
				// Re-submit would create a duplicate; skip for now.
				// TODO: implement proper update once UpdateTask RPC is added.
				return true; // Consider existing task as "synced"
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
	): Promise<boolean> {
		try {
			await this.rpc("WriteMemory", {
				key_scope: keyScope,
				key,
				content,
				content_type: contentType,
				source_agent: sourceAgent,
				task_id: taskId,
				project_id: projectId,
			});
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

	// ── Search Operations ──────────────────────────────────────

	async searchCode(
		query: string,
		modes: string[] = ["hybrid"],
		maxResults = 5,
	): Promise<Array<{ filePath: string; snippet: string; score: number }>> {
		try {
			const resp = await this.rpc("Search", {
				query,
				modes,
				max_results: maxResults,
			});
			return ((resp.items as RpcResp[]) ?? []).map((item) => ({
				filePath: (item.file_path as string) ?? "",
				snippet: (item.content_snippet as string) ?? "",
				score: (item.score as number) ?? 0,
			}));
		} catch {
			return [];
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
			"WatchTask", "PauseTask", "ResumeTask",
		]);
		const engineMethods = new Set([
			"Search", "IndexRepo", "GetIndexState", "RemoveIndex",
			"ReadMemory", "WriteMemory", "DeleteMemory", "SearchMemory",
			"Health", "BatchWriteMemory", "ListRepos", "ListDir", "GetFile",
		]);

		if (taskMethods.has(method)) return "TaskService";
		if (engineMethods.has(method)) return "EngineService";
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
