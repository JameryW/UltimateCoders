/**
 * GrpcBridge connection-error classification self-check.
 *
 * Two regressions fixed here:
 * 1. withReconnect/submitTask flipped connected=false and fired
 *    onConnectionChange(false) on ANY error — including gRPC business
 *    errors. That falsely signaled "UC: disconnected" on every rejected
 *    request even when the server was healthy.
 * 2. isConnectionError matched only low-level errno strings ("econnrefused"),
 *    but connectrpc surfaces a generic ConnectError ("Unable to connect...")
 *    wrapping the real cause. So a dead/restarted gRPC server was never
 *    classified as a connection drop — reconnect never fired and the error
 *    was misreported as a business rejection (submit_rejected) instead of
 *    server_unavailable.
 *
 * Run: bun test src/orchestrator/grpc-bridge.test.ts
 */

import { describe, expect, it } from "bun:test";
import { GrpcBridge } from "./grpc-bridge";

describe("GrpcBridge.isConnectionError", () => {
	it("flags transport-level errors as connection errors", () => {
		expect(GrpcBridge.isConnectionError(new Error("ECONNREFUSED"))).toBe(true);
		expect(GrpcBridge.isConnectionError(new Error("fetch failed: ECONNRESET"))).toBe(true);
		expect(GrpcBridge.isConnectionError(new Error("GOAWAY received"))).toBe(true);
		expect(GrpcBridge.isConnectionError(new Error("stream error"))).toBe(true);
		expect(GrpcBridge.isConnectionError(new Error("connection reset by peer"))).toBe(true);
		expect(GrpcBridge.isConnectionError(new Error("transport closed"))).toBe(true);
	});

	it("flags connectrpc's wrapped ConnectError (generic message, cause carries the code)", () => {
		// Real shape from connectrpc against a dead port.
		const cause = new Error("Unable to connect. Is the computer able to access the url?");
		(cause as { code?: string }).code = "ConnectionRefused";
		const connectErr = new Error("[unknown] Unable to connect. Is the computer able to access the url?", { cause });
		(connectErr as { name?: string }).name = "ConnectError";
		expect(GrpcBridge.isConnectionError(connectErr)).toBe(true);
	});

	it("does NOT flag gRPC business errors as connection errors", () => {
		// Server-reachable rejections — connection must stay up.
		expect(GrpcBridge.isConnectionError(new Error("not found"))).toBe(false);
		expect(GrpcBridge.isConnectionError(new Error("invalid argument: projectId"))).toBe(false);
		expect(GrpcBridge.isConnectionError(new Error("worker failed: no capacity"))).toBe(false);
		expect(GrpcBridge.isConnectionError(new Error("permission denied"))).toBe(false);
		expect(GrpcBridge.isConnectionError(new Error("engine rejected task"))).toBe(false);
	});

	it("rejects non-Error values", () => {
		expect(GrpcBridge.isConnectionError(null)).toBe(false);
		expect(GrpcBridge.isConnectionError(undefined)).toBe(false);
		expect(GrpcBridge.isConnectionError("ECONNREFUSED")).toBe(false);
		expect(GrpcBridge.isConnectionError({ message: "x" })).toBe(false);
	});
});

describe("GrpcBridge against a dead server", () => {
	// Port 1: nothing listens → every call is a transport error.
	const deadUrl = "http://127.0.0.1:1";

	it("classifies a dead server as server_unavailable (not submit_rejected)", async () => {
		const bridge = new GrpcBridge({ serverUrl: deadUrl });
		const result = await bridge.submitTask("test task");
		expect(result.ok).toBe(false);
		if (!result.ok) {
			expect(result.error.kind).toBe("server_unavailable");
		}
		bridge.close();
	});

	it("fires onConnectionChange(false) for a dead server", async () => {
		const seen: boolean[] = [];
		const bridge = new GrpcBridge({ serverUrl: deadUrl });
		bridge.setOnConnectionChange((c) => seen.push(c));
		await bridge.readMemory("scope", "key");
		expect(seen).toContain(false);
		bridge.close();
	});

	it("health() reports unavailable for a dead server", async () => {
		const bridge = new GrpcBridge({ serverUrl: deadUrl });
		const h = await bridge.health();
		expect(h.status).toBe("unavailable");
		bridge.close();
	});
});
