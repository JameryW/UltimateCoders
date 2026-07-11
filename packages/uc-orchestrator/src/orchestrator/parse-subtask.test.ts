/**
 * parseSubtaskOutput self-check — decomposer JSON → SubtaskDef[] mapping.
 *
 * This is the snake_case → field coercion path: the one place a malformed
 * decomposer payload silently corrupts the multi-agent step chain. Coercions
 * (String()/Boolean()) must hold for edge shapes; missing steps must stay
 * undefined (backward compat); non-JSON must fall back to text.
 *
 * Run: bun test src/orchestrator/parse-subtask.test.ts
 */

import { describe, expect, it } from "bun:test";
import { parseSubtaskOutput } from "./orchestrator";

describe("parseSubtaskOutput", () => {
	it("maps a 3-step chain with template variables", () => {
		const raw = JSON.stringify({
			subtasks: [
				{
					id: "st-2",
					description: "Implement auth middleware",
					depends_on: ["st-1"],
					files: ["src/auth/middleware.ts"],
					steps: [
						{ agent: "claude-code", prompt: "Implement JWT auth.", abort_on_failure: true },
						{ agent: "codex", prompt: "Review. {{prev_summary}} {{prev_files}}" },
						{ agent: "claude-code", prompt: "Revise. {{prev_summary}}", abort_on_failure: false },
					],
				},
			],
		});
		const defs = parseSubtaskOutput(raw);
		expect(defs.length).toBe(1);
		const st = defs[0];
		expect(st.id).toBe("st-2");
		expect(st.dependsOn).toEqual(["st-1"]);
		expect(st.steps).toBeDefined();
		expect(st.steps!.length).toBe(3);
		expect(st.steps![0]).toEqual({ agent: "claude-code", prompt: "Implement JWT auth.", abort_on_failure: true });
		expect(st.steps![1]).toEqual({ agent: "codex", prompt: "Review. {{prev_summary}} {{prev_files}}" });
		expect(st.steps![1].abort_on_failure).toBeUndefined();
		expect(st.steps![2].abort_on_failure).toBe(false);
	});

	it("steps undefined when subtask has no steps (backward compat)", () => {
		const raw = JSON.stringify({
			subtasks: [{ id: "st-1", description: "simple", depends_on: [], files: [] }],
		});
		const defs = parseSubtaskOutput(raw);
		expect(defs[0].steps).toBeUndefined();
	});

	it("coerces non-string agent/prompt to strings", () => {
		// ponytail: defensive coercion — decomposer schema enforcement may be loose
		const raw = JSON.stringify({
			subtasks: [{
				id: "st-1",
				description: "x",
				depends_on: [],
				files: [],
				steps: [{ agent: 123, prompt: 456, abort_on_failure: "true" }],
			}],
		});
		const defs = parseSubtaskOutput(raw);
		expect(defs[0].steps![0].agent).toBe("123");
		expect(defs[0].steps![0].prompt).toBe("456");
		expect(defs[0].steps![0].abort_on_failure).toBe(true);
	});

	it("preserves agent_config_json when present", () => {
		const raw = JSON.stringify({
			subtasks: [{
				id: "st-1",
				description: "x",
				depends_on: [],
				files: [],
				steps: [{ agent: "claude-code", prompt: "p", agent_config_json: '{"tools":["read"]}' }],
			}],
		});
		const defs = parseSubtaskOutput(raw);
		expect(defs[0].steps![0].agent_config_json).toBe('{"tools":["read"]}');
	});

	it("ignores unknown fields in step objects", () => {
		const raw = JSON.stringify({
			subtasks: [{
				id: "st-1",
				description: "x",
				depends_on: [],
				files: [],
				steps: [{ agent: "codex", prompt: "p", bogus_field: "ignored", timeout: 99 }],
			}],
		});
		const defs = parseSubtaskOutput(raw);
		const step = defs[0].steps![0];
		expect(Object.keys(step).sort()).toEqual(["agent", "prompt"]);
	});

	it("assigns default id st-N when missing", () => {
		const raw = JSON.stringify({
			subtasks: [{ description: "no id", depends_on: [], files: [] }],
		});
		const defs = parseSubtaskOutput(raw);
		expect(defs[0].id).toBe("st-1");
	});

	it("falls back to numbered-text parsing for non-JSON", () => {
		const raw = "1. First task\n2. Second task\n3. Third task";
		const defs = parseSubtaskOutput(raw);
		expect(defs.length).toBe(3);
		expect(defs[0].id).toBe("st-1");
		expect(defs[0].description).toBe("First task");
		expect(defs[0].dependsOn).toEqual([]);
		expect(defs[1].dependsOn).toEqual(["st-1"]);
		expect(defs[2].dependsOn).toEqual(["st-2"]);
		expect(defs.every((d) => d.steps === undefined)).toBe(true);
	});

	it("returns empty array for non-JSON without numbered lines", () => {
		expect(parseSubtaskOutput("just prose, no structure")).toEqual([]);
	});

	it("returns empty array for JSON without subtasks array", () => {
		expect(parseSubtaskOutput(JSON.stringify({ foo: "bar" }))).toEqual([]);
	});

	// ── requiredCapabilities derivation from steps[].agent ───────────

	it("derives requiredCapabilities from steps[].agent (union, deduped)", () => {
		const raw = JSON.stringify({
			subtasks: [
				{
					id: "st-1",
					description: "implement + codex CR",
					depends_on: [],
					files: [],
					steps: [
						{ agent: "claude-code", prompt: "Implement." },
						{ agent: "codex", prompt: "Review." },
						{ agent: "claude-code", prompt: "Revise." },
					],
				},
			],
		});
		const defs = parseSubtaskOutput(raw);
		expect(defs[0].requiredCapabilities).toEqual(["claude-code", "codex"]);
	});

	it("merges explicit required_capabilities with step agents (deduped)", () => {
		const raw = JSON.stringify({
			subtasks: [
				{
					id: "st-1",
					description: "explicit + steps",
					depends_on: [],
					files: [],
					required_capabilities: ["python", "claude-code"],
					steps: [
						{ agent: "claude-code", prompt: "Implement." },
						{ agent: "codex", prompt: "Review." },
					],
				},
			],
		});
		const defs = parseSubtaskOutput(raw);
		// Explicit caps first, then step agents, deduped
		expect(defs[0].requiredCapabilities).toEqual(["python", "claude-code", "codex"]);
	});

	it("skips empty agent strings when deriving capabilities", () => {
		const raw = JSON.stringify({
			subtasks: [
				{
					id: "st-1",
					description: "missing agent",
					depends_on: [],
					files: [],
					steps: [
						{ agent: "codex", prompt: "Review." },
						{ agent: "", prompt: "No agent." },
					],
				},
			],
		});
		const defs = parseSubtaskOutput(raw);
		// Only valid agent names added — empty string skipped
		expect(defs[0].requiredCapabilities).toEqual(["codex"]);
	});

	it("does not set requiredCapabilities when subtask has no steps", () => {
		const raw = JSON.stringify({
			subtasks: [
				{
					id: "st-1",
					description: "no steps",
					depends_on: [],
					files: [],
					required_capabilities: ["python"],
				},
			],
		});
		const defs = parseSubtaskOutput(raw);
		// Backward compat: no steps = requiredCapabilities left as whatever JSON provided
		// (parseSubtaskOutput only reads required_capabilities when steps are present)
		expect(defs[0].requiredCapabilities).toBeUndefined();
	});
});
