/**
 * Unit tests for uc-rpc-server JSONL dispatch.
 *
 * Exercises the REAL RpcServer.dispatch() (not a mirrored copy) so the
 * suite breaks if the server's dispatch drifts. Run via `bun test`.
 */

import { describe, expect, it } from "bun:test";
import { RpcServer } from "./uc-rpc-server";
import type { JsonRpcResponse } from "./uc-rpc-server";

// ponytail: dispatch a fresh server without init() — init() subscribes
// orchestrator events to stdout, which we don't want in unit tests.
async function dispatch(server: RpcServer, method: string, params: Record<string, unknown>, id?: number): Promise<JsonRpcResponse> {
	return server.dispatch({ method, params, id });
}

describe("RpcServer.dispatch", () => {
	it("list_tasks returns empty array initially", async () => {
		const server = new RpcServer();
		const resp = await dispatch(server, "list_tasks", {}, 1);
		expect(resp.id).toBe(1);
		expect(resp.error).toBeUndefined();
		expect(Array.isArray(resp.result?.tasks)).toBe(true);
		expect(resp.result?.tasks).toHaveLength(0);
	});

	it("get_task with missing id returns { task: null }", async () => {
		const server = new RpcServer();
		const resp = await dispatch(server, "get_task", { task_id: "nonexistent" }, 2);
		expect(resp.id).toBe(2);
		expect(resp.result?.task).toBeNull();
	});

	it("cancel_task with missing id returns ok=false", async () => {
		const server = new RpcServer();
		const resp = await dispatch(server, "cancel_task", { task_id: "nonexistent" }, 3);
		expect(resp.result?.ok).toBe(false);
	});

	it("pause_task with missing id returns ok=false", async () => {
		const server = new RpcServer();
		const resp = await dispatch(server, "pause_task", { task_id: "nonexistent" }, 4);
		expect(resp.result?.ok).toBe(false);
	});

	it("resume_task with missing id returns ok=false", async () => {
		const server = new RpcServer();
		const resp = await dispatch(server, "resume_task", { task_id: "nonexistent" }, 5);
		expect(resp.result?.ok).toBe(false);
	});

	it("unknown method returns error code -32000 with descriptive message", async () => {
		const server = new RpcServer();
		const resp = await dispatch(server, "bogus_method", {}, 6);
		expect(resp.error).toBeDefined();
		expect(resp.error?.code).toBe(-32000);
		expect(resp.error?.message).toContain("Unknown method");
	});

	it("show_status without task_id returns ok + tasks array", async () => {
		const server = new RpcServer();
		const resp = await dispatch(server, "show_status", {}, 7);
		expect(resp.result?.status).toBe("ok");
		expect(Array.isArray(resp.result?.tasks)).toBe(true);
	});

	it("show_status with missing task returns not_found", async () => {
		const server = new RpcServer();
		const resp = await dispatch(server, "show_status", { task_id: "nope" }, 8);
		expect(resp.result?.status).toBe("not_found");
	});

	it("shutdown returns ok=true", async () => {
		const server = new RpcServer();
		// ponytail: real scheduleShutdown() calls process.exit(0) on the next
		// tick via setImmediate — neutralize it so the test runner survives.
		let shutdownScheduled = false;
		server.scheduleShutdown = () => { shutdownScheduled = true; };
		const resp = await dispatch(server, "shutdown", {}, 9);
		expect(resp.result?.ok).toBe(true);
		expect(shutdownScheduled).toBe(true);
	});

	it("submit_task without description returns error mentioning description", async () => {
		const server = new RpcServer();
		const resp = await dispatch(server, "submit_task", {}, 10);
		expect(resp.error).toBeDefined();
		expect(resp.error?.message).toContain("description");
	});

	it("auto-assigns an id when the request omits one", async () => {
		const server = new RpcServer();
		const resp = await dispatch(server, "list_tasks", {});
		expect(typeof resp.id).toBe("number");
		expect(resp.id).toBeGreaterThan(0);
	});
});
