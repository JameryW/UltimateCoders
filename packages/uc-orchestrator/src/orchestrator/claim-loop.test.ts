/**
 * T6 #642 C4 — claim loop tests.
 *
 * The wave machine is gone: submitTask/runTask upsert all-Pending (the Rust
 * gateway publishes ready nodes to NATS workers) and the claim loop claims
 * whatever no worker took, executes it locally, and reports the outcome via
 * the UpdateTask upsert (no per-subtask RPC exists).
 *
 * The fake server applies the REAL wire mappings (taskStatusToWire /
 * subtaskStatusToWire from grpc-bridge) so every assertion below exercises
 * the exact strings the Rust gateway parses case-sensitively (fail-loud
 * since #350).
 *
 * Tests drive ticks manually via runClaimTick() (claimPollMs cranked to 1h —
 * no real-interval races) and await fire-and-forget executions with waitFor.
 *
 * Run: bun test src/orchestrator/claim-loop.test.ts
 */

import { afterEach, describe, expect, it, mock } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// ── Controllable runSubprocess mock ────────────────────────────────
// The decomposer call (agent.name === "decomposer") consumes decomposeQueue
// (raw JSON strings); subtask executions honor `execBehavior`, resolve the
// abort signal like the real runSubprocess, and pause on `heldExecution`.

let execBehavior: "succeed" | "fail" = "succeed";
let decomposeQueue: string[] = [];
let heldExecution: Promise<void> | null = null;

const DEFAULT_DEFS = JSON.stringify({
	subtasks: [{ id: "st-1", description: "first", depends_on: [], files: [] }],
});

mock.module("@oh-my-pi/pi-coding-agent", () => ({
	runSubprocess: async (opts: any) => {
		if (opts?.agent?.name === "decomposer") {
			const raw = decomposeQueue.shift() ?? DEFAULT_DEFS;
			return { exitCode: 0, stderr: "", output: raw };
		}
		// Abort-honoring delay — mirrors the real runSubprocess's signal
		// behavior closely enough for cancel-mid-execution tests. 15ms is
		// long enough that cancelTask's await chain reliably lands first.
		await new Promise<void>((resolve, reject) => {
			const t = setTimeout(() => resolve(), 15);
			opts?.signal?.addEventListener("abort", () => {
				clearTimeout(t);
				const e = new Error("aborted");
				e.name = "AbortError";
				reject(e);
			}, { once: true });
		});
		if (execBehavior === "fail") {
			return { exitCode: 1, stderr: "exec failed on purpose", output: "" };
		}
		if (heldExecution) await heldExecution;
		return { exitCode: 0, stderr: "", output: "exec ok" };
	},
}));

import { UCOrchestrator } from "./orchestrator";
import { taskStatusToWire, subtaskStatusToWire, type GrpcBridge } from "./grpc-bridge";
import type { PersistedTask } from "./task-store";

// ── Fake server (applies the real wire mappings) ───────────────────

interface ServerSub {
	id: string;
	description: string;
	status: string; // WIRE status ("Pending"/"InProgress"/…)
	dependsOn: string[];
	result?: string;
}

interface ServerTask {
	taskId: string;
	description: string;
	status: string;
	projectId: string;
	subtasks: ServerSub[];
}

class FakeServer {
	tasks = new Map<string, ServerTask>();
	upsertLog: Array<Record<string, string>> = []; // per upsert: subtaskId → wire status
	acceptUpserts = true;
	getTaskCalls = 0;

	applyUpsert(persisted: PersistedTask): boolean {
		const entry: Record<string, string> = {};
		for (const st of persisted.subtasks) entry[st.id] = subtaskStatusToWire(st.status);
		this.upsertLog.push(entry);
		if (!this.acceptUpserts) return false;

		let t = this.tasks.get(persisted.id);
		if (!t) {
			t = {
				taskId: persisted.id,
				description: persisted.description,
				status: taskStatusToWire(persisted.status),
				projectId: persisted.projectId ?? "",
				subtasks: [],
			};
			this.tasks.set(persisted.id, t);
		}
		t.status = taskStatusToWire(persisted.status);
		for (const st of persisted.subtasks) {
			const existing = t.subtasks.find((x) => x.id === st.id);
			const wire = subtaskStatusToWire(st.status);
			if (existing) {
				existing.status = wire;
				if (st.result) existing.result = st.result;
			} else {
				t.subtasks.push({ id: st.id, description: st.description, status: wire, dependsOn: st.dependsOn, result: st.result });
			}
		}
		return true;
	}

	snapshot(taskId: string): ServerTask | null {
		const t = this.tasks.get(taskId);
		// JSON round-trip keeps the fake honest about wire-shape drift.
		return t ? (JSON.parse(JSON.stringify(t)) as ServerTask) : null;
	}
}

function defsJson(subs: Array<{ id: string; description: string; depends_on?: string[]; files?: string[]; dispatchMode?: string }>): string {
	return JSON.stringify({
		// dispatch_mode rides the decomposer JSON protocol (snake_case, like
		// depends_on/files) and must survive parseSubtaskOutput → mirror.
		subtasks: subs.map((s) => ({ id: s.id, description: s.description, depends_on: s.depends_on ?? [], files: s.files ?? [], ...(s.dispatchMode ? { dispatch_mode: s.dispatchMode } : {}) })),
	});
}

// ── Harness ────────────────────────────────────────────────────────

interface Harness {
	orch: UCOrchestrator;
	server: FakeServer;
	warns: string[];
	events: { start: string[]; end: string[]; failed: string[]; complete: Array<{ taskId: string; status: string }> };
}

async function makeHarness(exec: { maxConcurrency?: number } = {}): Promise<Harness> {
	const workspace = mkdtempSync(join(tmpdir(), "uc-claim-loop-"));
	const server = new FakeServer();
	const warns: string[] = [];
	const pi = {
		pi: { settings: { workspaceRoot: workspace } },
		logger: { warn: (m: string) => warns.push(String(m)), info: () => {} },
		sendMessage: () => {},
	};
	const bridge = {
		isConnected: () => false,
		setOnConnectionChange: () => {},
		setOnReconnectAttempt: () => {},
		startWatchTask: () => ({ abort: () => {} }),
		getTask: (id: string) => { server.getTaskCalls++; return Promise.resolve(server.snapshot(id)); },
		upsertTask: (p: PersistedTask) => Promise.resolve(server.applyUpsert(p)),
		listTasks: () => Promise.resolve([]),
		writeMemory: () => Promise.resolve(true),
		// C3 RPC-first control verbs — the orchestrator awaits these before
		// touching the mirror; resolve success (fake server accepts all).
		pauseTask: () => Promise.resolve(true),
		resumeTask: () => Promise.resolve(true),
		// Task-level cancel is RPC-first: mimic the server (task → Failed,
		// non-terminal subtasks → Failed).
		cancelTask: (taskId: string) => {
			const t = server.tasks.get(taskId);
			if (t) {
				t.status = "Failed";
				for (const st of t.subtasks) {
					if (st.status !== "Completed") st.status = "Failed";
				}
			}
			return Promise.resolve(true);
		},
	} as unknown as GrpcBridge;

	const orch = new UCOrchestrator(pi as never, {
		claimPollMs: 3_600_000, // tests drive ticks manually — never fire
		maxRetries: 0,
		retryBaseDelayMs: 1,
		maxConcurrency: exec.maxConcurrency ?? 3,
	}, bridge);
	await orch.restore();

	const events = { start: [] as string[], end: [] as string[], failed: [] as string[], complete: [] as Array<{ taskId: string; status: string }> };
	orch.events.on("subtask_start", (d) => events.start.push(d.subtaskId));
	orch.events.on("subtask_end", (d) => events.end.push(d.subtaskId));
	orch.events.on("subtask_failed", (d) => events.failed.push(d.subtaskId));
	orch.events.on("task_complete", (d) => events.complete.push({ taskId: d.taskId, status: d.status }));

	return { orch, server, warns, events };
}

async function waitFor(fn: () => boolean, timeoutMs = 3000): Promise<void> {
	const start = Date.now();
	while (!fn()) {
		if (Date.now() - start > timeoutMs) throw new Error("waitFor timeout");
		await new Promise((r) => setTimeout(r, 10));
	}
}

const harnesses: Harness[] = [];
async function tracked(exec?: { maxConcurrency?: number }): Promise<Harness> {
	const h = await makeHarness(exec);
	harnesses.push(h);
	return h;
}

afterEach(async () => {
	while (harnesses.length > 0) {
		const h = harnesses.pop()!;
		execBehavior = "succeed";
		heldExecution = null;
		decomposeQueue = [];
		await h.orch.destroy().catch(() => {});
	}
});

// ── Wire mapping (the #350 breakage fix) ───────────────────────────

describe("status wire mapping", () => {
	it("maps mirror statuses to the server's case-sensitive vocabulary", () => {
		expect(taskStatusToWire("planning")).toBe("Planning");
		expect(taskStatusToWire("in_progress")).toBe("InProgress");
		expect(taskStatusToWire("completed")).toBe("Completed");
		expect(taskStatusToWire("failed")).toBe("Failed");
		// cancelled has no server variant — the authoritative CancelTask maps
		// to Failed server-side, so the mirror label rides the same semantics.
		expect(taskStatusToWire("cancelled")).toBe("Failed");

		expect(subtaskStatusToWire("pending")).toBe("Pending");
		expect(subtaskStatusToWire("running")).toBe("InProgress");
		expect(subtaskStatusToWire("assigned")).toBe("InProgress");
		expect(subtaskStatusToWire("completed")).toBe("Completed");
		expect(subtaskStatusToWire("failed")).toBe("Failed");
		expect(subtaskStatusToWire("cancelled")).toBe("Failed");
	});

	it("submitTask upserts all-Pending under the wire mapping (post-#350 accepted)", async () => {
		const { orch, server } = await tracked();
		decomposeQueue = [defsJson([
			{ id: "st-1", description: "first" },
			{ id: "st-2", description: "second", depends_on: ["st-1"] },
		])];
		const taskId = await orch.submitTask("wire mapping task");

		const t = server.tasks.get(taskId);
		expect(t).not.toBeNull();
		expect(t!.status).toBe("InProgress");
		expect(t!.subtasks.map((s) => s.status)).toEqual(["Pending", "Pending"]);
		// The submit-time upsert (first log entry) carried wire statuses.
		expect(server.upsertLog[0]).toEqual({ "st-1": "Pending", "st-2": "Pending" });
	});
});

// ── Claim loop behavior ────────────────────────────────────────────

describe("claim loop", () => {
	it("claims a ready node, executes it, reports Completed, then finishes the task", async () => {
		const { orch, server, events } = await tracked();
		const taskId = await orch.submitTask("single subtask task");
		const task = orch.getTaskState(taskId)!;

		await orch.runClaimTick(); // claim + fire-and-forget execute
		expect(events.start).toEqual(["st-1"]);
		expect(server.snapshot(taskId)!.subtasks[0].status).toBe("InProgress");
		expect(task.subtasks[0].status).toBe("running");

		await waitFor(() => server.snapshot(taskId)!.subtasks[0].status === "Completed");
		expect(events.end).toEqual(["st-1"]);
		expect(task.subtasks[0].status).toBe("completed");
		expect(task.subtasks[0].result).toBe("exec ok");

		await orch.runClaimTick(); // terminal inference
		expect(task.status).toBe("completed");
		expect(events.complete).toEqual([{ taskId, status: "completed" }]);
	});

	it("gates claims on deps being Completed on the SERVER snapshot", async () => {
		const { orch, server } = await tracked();
		decomposeQueue = [defsJson([
			{ id: "st-1", description: "first" },
			{ id: "st-2", description: "second", depends_on: ["st-1"] },
		])];
		const taskId = await orch.submitTask("dependency task");
		const task = orch.getTaskState(taskId)!;

		await orch.runClaimTick(); // claims st-1 only
		expect(task.subtasks.find((s) => s.id === "st-1")!.status).toBe("running");
		expect(task.subtasks.find((s) => s.id === "st-2")!.status).toBe("pending");
		expect(server.snapshot(taskId)!.subtasks[1].status).toBe("Pending");

		await waitFor(() => server.snapshot(taskId)!.subtasks[0].status === "Completed");

		await orch.runClaimTick(); // st-1 completed server-side → st-2 ready
		expect(task.subtasks.find((s) => s.id === "st-2")!.status).toBe("running");
		await waitFor(() => server.snapshot(taskId)!.subtasks[1].status === "Completed");

		await orch.runClaimTick();
		expect(task.status).toBe("completed");
	});

	it("never claims remote-only subtasks (worker territory)", async () => {
		const { orch, server, events } = await tracked();
		decomposeQueue = [defsJson([{ id: "st-1", description: "remote only", dispatchMode: "remote" }])];
		const taskId = await orch.submitTask("remote-only task");
		const task = orch.getTaskState(taskId)!;

		await orch.runClaimTick();
		await orch.runClaimTick();
		expect(task.subtasks[0].status).toBe("pending");
		expect(server.snapshot(taskId)!.subtasks[0].status).toBe("Pending");
		expect(events.start).toEqual([]); // never even started locally
	});

	it("adopts worker-completed subtasks from the server snapshot", async () => {
		const { orch, server } = await tracked();
		decomposeQueue = [defsJson([{ id: "st-1", description: "worker does it" }])];
		const taskId = await orch.submitTask("adoption task");
		const task = orch.getTaskState(taskId)!;

		// A worker completed it between submit and the next tick.
		const st = server.tasks.get(taskId)!.subtasks[0];
		st.status = "Completed";
		st.result = "worker did it";

		await orch.runClaimTick();
		expect(task.subtasks[0].status).toBe("completed");
		expect(task.subtasks[0].result).toBe("worker did it");

		await orch.runClaimTick();
		expect(task.status).toBe("completed");
	});

	it("mirrors server InProgress as running (worker took it)", async () => {
		const { orch, server } = await tracked();
		decomposeQueue = [defsJson([{ id: "st-1", description: "worker runs it" }])];
		const taskId = await orch.submitTask("assigned task");
		const task = orch.getTaskState(taskId)!;

		server.tasks.get(taskId)!.subtasks[0].status = "InProgress";
		await orch.runClaimTick();
		expect(task.subtasks[0].status).toBe("running");
		// Still open — the task must not complete while the worker runs it.
		expect(task.status).toBe("in_progress");
	});

	it("re-decomposes a failed set once, then completes via the new nodes", async () => {
		const { orch, server } = await tracked();
		execBehavior = "fail";
		decomposeQueue = [
			defsJson([{ id: "st-1", description: "will fail" }]),
			defsJson([{ id: "st-1r", description: "smaller retry" }]), // consumed by tryRedecompose
		];
		const taskId = await orch.submitTask("redecompose task");
		const task = orch.getTaskState(taskId)!;

		await orch.runClaimTick(); // claim + execute (fails) + report Failed → kick
		await waitFor(() => server.snapshot(taskId)!.subtasks[0].status === "Failed");
		// The kick after the reported outcome runs the reconcile tick in the
		// background: terminal inference fires tryRedecompose, which REMOVES
		// the failed subtasks and appends the new Pending set — observe it
		// instead of assuming the next manual tick is the one to do it.
		await waitFor(() => task.redecomposed === true);
		expect(task.subtasks.some((s) => s.id === "st-1r" && s.status === "pending")).toBe(true);
		expect(task.subtasks.some((s) => s.status === "failed")).toBe(false); // failed set replaced
		// tryRedecompose's tick returns right after the upsert — no claim kick
		// follows, so st-1r stays Pending until we drive the next tick.
		expect(task.status).toBe("in_progress");

		execBehavior = "succeed";
		await orch.runClaimTick(); // claims st-1r
		await waitFor(() => server.snapshot(taskId)!.subtasks.some((s) => s.id === "st-1r" && s.status === "Completed"));
		await waitFor(() => task.status === "completed"); // finishTask rides the outcome kick
	});

	it("reverts the claim when the server rejects the running report, then retries next tick", async () => {
		const { orch, server } = await tracked();
		const taskId = await orch.submitTask("claim-reject task");
		const task = orch.getTaskState(taskId)!;

		server.acceptUpserts = false;
		await orch.runClaimTick(); // claim report rejected ×3 → revert
		expect(task.subtasks[0].status).toBe("pending");
		expect(server.snapshot(taskId)!.subtasks[0].status).toBe("Pending");

		server.acceptUpserts = true;
		await orch.runClaimTick();
		expect(task.subtasks[0].status).toBe("running");
		await waitFor(() => server.snapshot(taskId)!.subtasks[0].status === "Completed");
	});

	it("survives a finally-failed outcome report: unclaims, keeps the outcome, recovers next tick", async () => {
		const { orch, server, warns } = await tracked();
		const taskId = await orch.submitTask("report-failure task");
		const task = orch.getTaskState(taskId)!;

		// Hold the execution mid-flight so we can kill the report channel.
		let release!: () => void;
		heldExecution = new Promise<void>((r) => { release = r; });

		await orch.runClaimTick(); // claims (report OK) + execution starts (held)
		expect(task.subtasks[0].status).toBe("running");

		server.acceptUpserts = false;
		release(); // execution completes; outcome report now fails ×3
		await waitFor(() => task.subtasks[0].status === "completed");
		// The report exhausts its retries (~1s), unclaims, and warns loudly.
		// Waiting on the warn (emitted right after the unclaim) keeps the
		// recovery tick below from racing the in-flight report loop.
		await waitFor(() => warns.some((w) => w.includes("outcome report failed")));
		// Unclaimed + mirror keeps the outcome; server still shows InProgress.
		expect(server.snapshot(taskId)!.subtasks[0].status).toBe("InProgress");

		server.acceptUpserts = true;
		await orch.runClaimTick(); // nothing open → task completes; final sync pushes Completed
		expect(task.status).toBe("completed");
		expect(server.snapshot(taskId)!.subtasks[0].status).toBe("Completed");
	});

	it("respects maxConcurrency — one claim per tick when capped at 1", async () => {
		const { orch, server } = await tracked({ maxConcurrency: 1 });
		decomposeQueue = [defsJson([
			{ id: "st-1", description: "a" },
			{ id: "st-2", description: "b" },
		])];
		const taskId = await orch.submitTask("cap task");
		const task = orch.getTaskState(taskId)!;

		await orch.runClaimTick();
		const running = task.subtasks.filter((s) => s.status === "running").length;
		expect(running).toBe(1);
		expect(server.snapshot(taskId)!.subtasks.filter((s) => s.status === "InProgress").length).toBe(1);

		await waitFor(() => server.snapshot(taskId)!.subtasks.every((s) => s.status === "Completed"));
		await orch.runClaimTick();
		expect(task.status).toBe("completed");
	});

	it("does not own paused tasks — no claims while the dispatch gate is closed", async () => {
		const { orch, server } = await tracked();
		const taskId = await orch.submitTask("paused task");
		const task = orch.getTaskState(taskId)!;

		const p = await orch.pauseTask(taskId);
		expect(p.ok).toBe(true);

		await orch.runClaimTick();
		expect(task.subtasks[0].status).toBe("pending");
		expect(server.snapshot(taskId)!.subtasks[0].status).toBe("Pending");
	});

	it("cancel is RPC-first: server rows go Failed, mirror cancelled, claims released", async () => {
		const { orch, server, events } = await tracked();
		const taskId = await orch.submitTask("cancel task");
		const task = orch.getTaskState(taskId)!;

		await orch.runClaimTick(); // claim st-1 (in flight on the fake server)
		expect(server.snapshot(taskId)!.subtasks[0].status).toBe("InProgress");

		const r = await orch.cancelTask(taskId);
		expect(r.ok).toBe(true);
		expect(task.status).toBe("cancelled");
		expect(task.controlState).toBe("cancelled");
		expect(task.subtasks[0].status).toBe("cancelled");
		// Server authority: task + non-terminal subtasks → Failed.
		expect(server.snapshot(taskId)!.status).toBe("Failed");
		expect(server.snapshot(taskId)!.subtasks[0].status).toBe("Failed");
		expect(events.complete.map((c) => c.status)).not.toContain("completed");

		// The in-flight execution settles into its aborted signal (mock honors
		// the abort → AbortError → cancelled result) — no crash, no
		// resurrection of the task.
		await new Promise((r) => setTimeout(r, 40)); // aborted run settles
		await orch.runClaimTick(); // reconcile guard rejects the cancelled task
		expect(task.status).toBe("cancelled");
		expect(task.subtasks[0].status).toBe("cancelled");
		expect(server.snapshot(taskId)!.subtasks[0].status).toBe("Failed");
	});

	it("idle ticks self-stop the loop without touching the bridge", async () => {
		const { orch, server } = await tracked();
		await orch.runClaimTick();
		expect(server.getTaskCalls).toBe(0); // nothing owned → no snapshots fetched
	});
});

// ── C5 conflict_risk gating ────────────────────────────────────────

describe("conflict risk claim gating", () => {
	it("low-risk nodes claim freely in one tick", async () => {
		const { orch, server } = await tracked();
		decomposeQueue = [defsJson([
			{ id: "st-1", description: "a", files: ["a.ts"] },
			{ id: "st-2", description: "b", files: ["b.ts"] },
		])];
		const taskId = await orch.submitTask("free parallel task");
		const task = orch.getTaskState(taskId)!;
		expect(task.subtasks.map((s) => s.conflictRisk)).toEqual(["low", "low"]);

		await orch.runClaimTick();
		expect(task.subtasks.every((s) => s.status === "running")).toBe(true);
		await waitFor(() => server.snapshot(taskId)!.subtasks.every((s) => s.status === "Completed"));
	});

	it("high-risk nodes run alone — nothing claims alongside, next node waits for the set to drain", async () => {
		const { orch, server } = await tracked();
		decomposeQueue = [defsJson([
			{ id: "st-1", description: "hot", files: ["shared.ts", "x.ts"] },
			{ id: "st-2", description: "also hot", files: ["shared.ts", "x.ts"] }, // overlap 1.0 → high
		])];
		const taskId = await orch.submitTask("high risk task");
		const task = orch.getTaskState(taskId)!;
		expect(task.subtasks.map((s) => s.conflictRisk)).toEqual(["high", "high"]);

		await orch.runClaimTick();
		expect(task.subtasks[0].status).toBe("running");
		expect(task.subtasks[1].status).toBe("pending"); // high yields the whole batch

		await waitFor(() => server.snapshot(taskId)!.subtasks[0].status === "Completed");
		await orch.runClaimTick(); // parallel set drained → st-2 may run alone
		expect(task.subtasks[1].status).toBe("running");
		await waitFor(() => server.snapshot(taskId)!.subtasks[1].status === "Completed");
	});

	it("medium-risk nodes claim at most one per parallel set", async () => {
		const { orch, server } = await tracked();
		decomposeQueue = [defsJson([
			{ id: "st-1", description: "warm a", files: ["w.ts", "a.ts"] },
			{ id: "st-2", description: "warm b", files: ["w.ts", "b.ts"] },
		])];
		const taskId = await orch.submitTask("medium risk task");
		const task = orch.getTaskState(taskId)!;
		expect(task.subtasks.map((s) => s.conflictRisk)).toEqual(["medium", "medium"]);

		await orch.runClaimTick();
		expect(task.subtasks[0].status).toBe("running");
		expect(task.subtasks[1].status).toBe("pending"); // one medium already in the set

		await waitFor(() => server.snapshot(taskId)!.subtasks[0].status === "Completed");
		await orch.runClaimTick();
		expect(task.subtasks[1].status).toBe("running");
		await waitFor(() => server.snapshot(taskId)!.subtasks[1].status === "Completed");
	});
});
