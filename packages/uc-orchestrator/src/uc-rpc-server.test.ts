/**
 * Unit tests for uc-rpc-server JSONL dispatch.
 *
 * Run: bun run packages/uc-orchestrator/src/uc-rpc-server.test.ts
 */

import { UCOrchestrator, type TaskState } from "./orchestrator/orchestrator";
import { GrpcBridge } from "./orchestrator/grpc-bridge";
import type { ExtensionAPI, ExtensionCommandContext } from "@oh-my-pi/pi-coding-agent";

// ── Minimal stubs ──────────────────────────────────────────────────

const stubPi: ExtensionAPI = {
	pi: { settings: { workspaceRoot: "/tmp/uc-test" } },
	logger: {
		info: () => {},
		warn: () => {},
		error: () => {},
	},
} as unknown as ExtensionAPI;

function stubCtx(): ExtensionCommandContext {
	return {
		cwd: "/tmp/uc-test",
		ui: { notify: () => {}, setWidget: () => {} },
	} as unknown as ExtensionCommandContext;
}

// ── Dispatch logic (extracted for testability) ─────────────────────

// ponytail: mirror the server's handleMethod without stdio I/O

interface JsonRpcRequest {
	method: string;
	params: Record<string, unknown>;
	id?: number;
}

function serializeTask(task: TaskState): Record<string, unknown> {
	return {
		id: task.id,
		description: task.description,
		status: task.status,
		controlState: task.controlState,
		createdAt: task.createdAt,
		completedAt: task.completedAt,
		error: task.error,
		subtasks: task.subtasks.map((st) => ({
			id: st.id,
			description: st.description,
			status: st.status,
			dependsOn: st.dependsOn,
			result: st.result,
			error: st.error,
		})),
	};
}

async function handleMethod(
	orchestrator: UCOrchestrator,
	method: string,
	params: Record<string, unknown>,
): Promise<unknown> {
	switch (method) {
		case "submit_task": {
			const description = String(params.description ?? "");
			if (!description) throw new Error("description is required");
			const taskId = await orchestrator.submitTask(description, stubCtx());
			return { task_id: taskId };
		}
		case "cancel_task": {
			const taskId = String(params.task_id ?? "");
			if (!taskId) throw new Error("task_id is required");
			const ok = await orchestrator.cancelTask(taskId, params.subtask_id as string | undefined);
			return { ok };
		}
		case "pause_task": {
			const taskId = String(params.task_id ?? "");
			if (!taskId) throw new Error("task_id is required");
			const ok = await orchestrator.pauseTask(taskId);
			return { ok };
		}
		case "resume_task": {
			const taskId = String(params.task_id ?? "");
			if (!taskId) throw new Error("task_id is required");
			const ok = await orchestrator.resumeTask(taskId);
			return { ok };
		}
		case "show_status": {
			const taskId = params.task_id ? String(params.task_id) : undefined;
			if (!taskId) {
				const tasks = orchestrator.getAllTaskStates();
				return {
					status: "ok",
					tasks: tasks.map((t) => ({
						id: t.id,
						status: t.status,
						controlState: t.controlState,
						description: t.description.slice(0, 60),
						subtaskProgress: `${t.subtasks.filter((s) => s.status === "completed").length}/${t.subtasks.length}`,
					})),
				};
			}
			const task = orchestrator.getTaskState(taskId);
			if (!task) return { status: "not_found" };
			return { status: "ok", task: serializeTask(task) };
		}
		case "get_task": {
			const taskId = String(params.task_id ?? "");
			if (!taskId) throw new Error("task_id is required");
			const task = orchestrator.getTaskState(taskId);
			if (!task) return { task: null };
			return { task: serializeTask(task) };
		}
		case "list_tasks": {
			const tasks = orchestrator.getAllTaskStates();
			return { tasks: tasks.map(serializeTask) };
		}
		case "shutdown": {
			return { ok: true };
		}
		default:
			throw new Error(`Unknown method: ${method}`);
	}
}

async function dispatch(
	orchestrator: UCOrchestrator,
	req: JsonRpcRequest,
): Promise<{ id: number; result?: unknown; error?: { code: number; message: string } }> {
	const id = req.id ?? 0;
	try {
		const result = await handleMethod(orchestrator, req.method, req.params);
		return { id, result };
	} catch (err) {
		return {
			id,
			error: {
				code: -32000,
				message: err instanceof Error ? err.message : String(err),
			},
		};
	}
}

// ── Tests ──────────────────────────────────────────────────────────

async function runTests(): Promise<void> {
	let passed = 0;
	let failed = 0;

	function assert(condition: boolean, msg: string): void {
		if (condition) {
			passed++;
		} else {
			failed++;
			console.error(`FAIL: ${msg}`);
		}
	}

	// Setup: fresh orchestrator
	const bridge = new GrpcBridge();
	const orch = new UCOrchestrator(stubPi, undefined, bridge);

	// Test 1: list_tasks returns empty
	{
		const resp = await dispatch(orch, { method: "list_tasks", params: {}, id: 1 });
		assert(resp.id === 1, "list_tasks id matches");
		assert(Array.isArray((resp.result as any)?.tasks), "list_tasks returns tasks array");
		assert(((resp.result as any)?.tasks as unknown[]).length === 0, "list_tasks is empty initially");
	}

	// Test 2: get_task with missing id returns null
	{
		const resp = await dispatch(orch, { method: "get_task", params: { task_id: "nonexistent" }, id: 2 });
		assert(resp.id === 2, "get_task id matches");
		assert((resp.result as any)?.task === null, "get_task returns null for missing task");
	}

	// Test 3: cancel_task with missing id returns ok=false
	{
		const resp = await dispatch(orch, { method: "cancel_task", params: { task_id: "nonexistent" }, id: 3 });
		assert(resp.id === 3, "cancel_task id matches");
		assert((resp.result as any)?.ok === false, "cancel_task returns false for missing task");
	}

	// Test 4: pause_task with missing id returns ok=false
	{
		const resp = await dispatch(orch, { method: "pause_task", params: { task_id: "nonexistent" }, id: 4 });
		assert((resp.result as any)?.ok === false, "pause_task returns false for missing task");
	}

	// Test 5: resume_task with missing id returns ok=false
	{
		const resp = await dispatch(orch, { method: "resume_task", params: { task_id: "nonexistent" }, id: 5 });
		assert((resp.result as any)?.ok === false, "resume_task returns false for missing task");
	}

	// Test 6: unknown method returns error
	{
		const resp = await dispatch(orch, { method: "bogus_method", params: {}, id: 6 });
		assert(resp.error !== undefined, "unknown method returns error");
		assert(resp.error?.code === -32000, "error code is -32000");
		assert(!!resp.error?.message?.includes("Unknown method"), "error message mentions unknown method");
	}

	// Test 7: show_status without task_id returns list
	{
		const resp = await dispatch(orch, { method: "show_status", params: {}, id: 7 });
		assert((resp.result as any)?.status === "ok", "show_status returns ok");
		assert(Array.isArray((resp.result as any)?.tasks), "show_status returns tasks array");
	}

	// Test 8: show_status with missing task returns not_found
	{
		const resp = await dispatch(orch, { method: "show_status", params: { task_id: "nope" }, id: 8 });
		assert((resp.result as any)?.status === "not_found", "show_status returns not_found");
	}

	// Test 9: shutdown returns ok=true
	{
		const resp = await dispatch(orch, { method: "shutdown", params: {}, id: 9 });
		assert((resp.result as any)?.ok === true, "shutdown returns ok");
	}

	// Test 10: missing required params
	{
		const resp = await dispatch(orch, { method: "submit_task", params: {}, id: 10 });
		assert(resp.error !== undefined, "submit_task without description returns error");
		assert(!!resp.error?.message?.includes("description"), "error mentions description");
	}

	console.log(`\n${passed} passed, ${failed} failed`);
	if (failed > 0) process.exit(1);
}

runTests().catch((err) => {
	console.error("Test runner failed:", err);
	process.exit(1);
});
