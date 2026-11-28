/**
 * Tests for NanoHarness routing and MCP error normalisation (#50).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	COMPONENT_GAINS,
	COMPONENT_ORDER,
	NanoHarnessRouter,
	guideMcpError,
} from "./nano_harness_router";

describe("NanoHarness component routing", () => {
	it("keeps interface components and drops compensation for strong models", () => {
		const plan = new NanoHarnessRouter().route("strong", "repo-generation");
		assert.equal(plan.components.length, 5);
		assert.equal(plan.interfaceMode, "structured");
		assert.equal(plan.errorMode, "actionable");
		const byId = new Map(plan.components.map((component) => [component.id, component]));
		assert.equal(byId.get("tool-registry")?.enabled, true);
		assert.equal(byId.get("task-subagents")?.enabled, true);
		assert.equal(byId.get("generic-subagents")?.enabled, false);
		assert.equal(byId.get("lossy-compaction")?.enabled, false);
		assert.equal(byId.get("compensation-prompts")?.enabled, false);
	});

	it("gives the smallest model a bounded didactic fallback", () => {
		const plan = new NanoHarnessRouter().route("small", "bug-fix");
		const compensation = plan.components.find((item) => item.id === "compensation-prompts");
		assert.equal(compensation?.enabled, true);
	});

	it("disables task subagents for localized edits and test generation", () => {
		const router = new NanoHarnessRouter();
		assert.equal(
			router.decision(router.route("mid", "localized-edit"), "task-subagents")?.enabled,
			false,
		);
		assert.equal(
			router.decision(router.route("strong", "test-generation"), "task-subagents")?.enabled,
			false,
		);
	});

	it("keeps the tool registry enabled for every tier and task", () => {
		const router = new NanoHarnessRouter();
		for (const tier of ["small", "mid", "strong"] as const) {
			for (const task of [
				"repo-generation",
				"bug-fix",
				"localized-edit",
				"test-generation",
			] as const) {
				assert.equal(router.decision(router.route(tier, task), "tool-registry")?.enabled, true);
			}
		}
	});

	it("serialises the plan in a stable order", () => {
		const router = new NanoHarnessRouter();
		const plan = router.route("mid", "bug-fix");
		const parsed = JSON.parse(router.toJSON(plan)) as {
			components: { id: string }[];
		};
		assert.deepEqual(
			parsed.components.map((component) => component.id),
			[...COMPONENT_ORDER],
		);
		assert.equal(router.decision(plan, "missing" as never), undefined);
	});

	it("rejects unknown tiers and tasks", () => {
		const router = new NanoHarnessRouter();
		assert.throws(() => router.route("ultra" as never, "bug-fix"), /model tier/);
		assert.throws(() => router.route("strong", "deploy" as never), /task kind/);
	});

	it("publishes the measured component gains", () => {
		assert.equal(COMPONENT_GAINS["repo-generation"]["tool-registry"], 4.57);
		assert.equal(COMPONENT_GAINS["repo-generation"]["task-subagents"], 5.91);
		assert.equal(COMPONENT_GAINS["bug-fix"]["tool-registry"], 3.54);
	});
});

describe("MCP error normalisation", () => {
	it("turns missing paths into an executable repair action", () => {
		const guidance = guideMcpError("Error: ENOENT: no such file or directory, open 'src/a.ts'");
		assert.equal(guidance.code, "ENOENT");
		assert.match(guidance.action, /workspace root/);
		assert.equal(guidance.executable, true);
	});

	it("classifies permission, timeout, unknown tool and schema errors", () => {
		assert.equal(guideMcpError("EACCES: permission denied").code, "EACCES");
		assert.equal(guideMcpError("request timed out after 30s").code, "TIMEOUT");
		assert.equal(guideMcpError("unknown tool: repo_map").code, "UNKNOWN_TOOL");
		assert.equal(guideMcpError("invalid argument: path must be a string").code, "INVALID_ARGUMENT");
	});

	it("keeps an unclassified error honest rather than inventing a fix", () => {
		const guidance = guideMcpError("the server returned HTTP 500");
		assert.equal(guidance.code, "UNCLASSIFIED");
		assert.equal(guidance.executable, false);
		assert.match(guidance.action, /smallest valid input/);
	});
});
