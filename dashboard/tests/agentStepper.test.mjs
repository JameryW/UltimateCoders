import assert from "node:assert/strict";
import { test } from "node:test";

import { AgentStepper } from "../src/lib/agentStepper.ts";

function event(seq, kind, summary = `e${seq}`) {
	return { seq, kind, summary, diff: `diff-${seq}` };
}

test("starts paused", () => {
	const stepper = new AgentStepper();
	assert.equal(stepper.state, "paused");
});

test("resume and pause transition", () => {
	const stepper = new AgentStepper();
	stepper.resume();
	assert.equal(stepper.state, "running");
	stepper.pause();
	assert.equal(stepper.state, "paused");
});

test("breakpoint pauses a running machine", () => {
	const stepper = new AgentStepper();
	assert.equal(stepper.addBreakpoint("before_tool_call"), true);
	assert.equal(stepper.addBreakpoint("before_tool_call"), false);
	stepper.resume();
	const result = stepper.ingest(event(1, "before_tool_call"));
	assert.equal(result.pausedByBreakpoint, true);
	assert.equal(result.state, "paused");
});

test("after-tool-call creates parented shadow commits", () => {
	const stepper = new AgentStepper();
	const first = stepper.ingest(event(1, "after_tool_call")).commit;
	const second = stepper.ingest(event(2, "after_tool_call")).commit;
	assert.equal(first.parent, null);
	assert.equal(second.parent, first.id);
	assert.equal(stepper.commits.length, 2);
});

test("step runs one event then pauses", () => {
	const stepper = new AgentStepper();
	assert.equal(stepper.step(), true);
	assert.equal(stepper.state, "stepping");
	const result = stepper.ingest(event(1, "before_llm_query"));
	assert.equal(result.state, "paused");
	assert.equal(stepper.step(), true);
});

test("breakpoints can be removed", () => {
	const stepper = new AgentStepper();
	stepper.addBreakpoint("after_llm_query");
	assert.deepEqual(stepper.breakpoints, ["after_llm_query"]);
	assert.equal(stepper.removeBreakpoint("after_llm_query"), true);
	assert.deepEqual(stepper.breakpoints, []);
});

test("hot edits are bounded and replaceable", () => {
	const stepper = new AgentStepper(8, 8, 1);
	stepper.hotEdit("prompt", "new");
	stepper.hotEdit("prompt", "newer");
	assert.equal(stepper.overrides.length, 1);
	assert.equal(stepper.overrides[0].value, "newer");
	assert.throws(() => stepper.hotEdit("tool.args", "x"), /capacity/);
	assert.throws(() => stepper.hotEdit(" ", "x"), /must not be empty/);
});

test("event capacity is enforced", () => {
	const stepper = new AgentStepper(1, 8, 8);
	stepper.ingest(event(1, "before_llm_query"));
	assert.throws(() => stepper.ingest(event(2, "before_llm_query")), /capacity/);
});

test("commit capacity is enforced", () => {
	const stepper = new AgentStepper(8, 1, 8);
	stepper.ingest(event(1, "after_tool_call"));
	assert.throws(() => stepper.ingest(event(2, "after_tool_call")), /capacity/);
});

test("empty summary is rejected", () => {
	const stepper = new AgentStepper();
	assert.throws(
		() => stepper.ingest({ seq: 1, kind: "before_llm_query", summary: " " }),
		/must not be empty/,
	);
});
