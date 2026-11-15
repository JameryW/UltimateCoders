import assert from "node:assert/strict";
import { test } from "node:test";

import {
  cleanReplaySummary,
  fetchWorkflowReplays,
  formatReplayDuration,
  normaliseReplayPayload,
  normaliseReplayStatus,
  WorkflowReplayRequestError,
} from "../src/lib/replays.ts";

const PAYLOAD = {
  available: true,
  source: "nats",
  runs: [
    {
      id: "task-1:sub-1",
      task_id: "task-1",
      subtask_id: "sub-1",
      status: "completed",
      step_total: 2,
      completed_steps: 2,
      percent: 100,
      started_at: "2026-11-15T12:00:00+00:00",
      updated_at: "2026-11-15T12:02:00+00:00",
      steps: [
        {
          index: 1,
          agent: "reviewer",
          status: "completed",
          summary: "reviewed",
          retry_attempt: 1,
          parallel_group: "read",
          started_at: "2026-11-15T12:01:00+00:00",
          finished_at: "2026-11-15T12:02:00+00:00",
          duration_ms: 60000,
        },
        {
          index: 0,
          agent: "codex",
          status: "completed",
          summary: "wrote the file",
          duration_ms: 30000,
        },
      ],
    },
  ],
};

test("wire payload is normalised into ordered replay runs", () => {
  const runs = normaliseReplayPayload(PAYLOAD);

  assert.equal(runs.length, 1);
  assert.equal(runs[0].id, "task-1:sub-1");
  assert.equal(runs[0].status, "completed");
  assert.equal(runs[0].stepTotal, 2);
  assert.equal(runs[0].completedSteps, 2);
  assert.equal(runs[0].steps[0].index, 0);
  assert.equal(runs[0].steps[0].agent, "codex");
  assert.equal(runs[0].steps[1].retryAttempt, 1);
  assert.equal(runs[0].steps[1].parallelGroup, "read");
  assert.equal(runs[0].steps[1].durationMs, 60000);
});

test("malformed runs and steps are dropped, not rendered as empty runs", () => {
  const runs = normaliseReplayPayload({
    runs: [
      { status: "completed", steps: [] },
      { task_id: "t", steps: [{ index: "bad" }, { index: 0, status: "completed" }] },
      null,
    ],
  });

  assert.equal(runs.length, 1);
  assert.equal(runs[0].taskId, "t");
  assert.equal(runs[0].steps.length, 1);
  assert.equal(runs[0].stepTotal, 1);
});

test("unknown statuses degrade to the neutral status", () => {
  assert.equal(normaliseReplayStatus("failed"), "failed");
  assert.equal(normaliseReplayStatus("weird"), "unknown");
  assert.equal(normaliseReplayStatus(undefined), "unknown");
});

test("fetchWorkflowReplays sends the bearer token and parses runs", async () => {
  let captured = null;
  const fetchImpl = async (url, init) => {
    captured = { url, init };
    return { ok: true, status: 200, json: async () => PAYLOAD };
  };

  const runs = await fetchWorkflowReplays(fetchImpl, "token-123");

  assert.equal(captured.url, "/dashboard/api/workflows/replays");
  assert.equal(captured.init.headers.Authorization, "Bearer token-123");
  assert.equal(captured.init.headers.Accept, "application/json");
  assert.equal(runs[0].steps.length, 2);
});

test("fetchWorkflowReplays leaves the header off when no token is stored", async () => {
  let captured = null;
  const fetchImpl = async (url, init) => {
    captured = { url, init };
    return { ok: true, status: 200, json: async () => ({ runs: [] }) };
  };

  await fetchWorkflowReplays(fetchImpl);
  assert.equal(captured.init.headers.Authorization, undefined);
});

test("fetchWorkflowReplays rejects non-OK and malformed answers", async () => {
  await assert.rejects(
    fetchWorkflowReplays(async () => ({ ok: false, status: 401, json: async () => ({}) })),
    (error) => {
      assert.ok(error instanceof WorkflowReplayRequestError);
      assert.equal(error.status, 401);
      return /Replay request failed \(401\)/.test(error.message);
    },
  );
  await assert.rejects(
    fetchWorkflowReplays(async () => ({ ok: true, status: 200, json: async () => ({ runs: "no" }) })),
    /Replay response is malformed/,
  );
});

test("durations are human readable at every scale", () => {
  assert.equal(formatReplayDuration(null), "—");
  assert.equal(formatReplayDuration(-1), "—");
  assert.equal(formatReplayDuration(900), "900ms");
  assert.equal(formatReplayDuration(1500), "1.5s");
  assert.equal(formatReplayDuration(65_000), "1m 05s");
});

test("step summaries lose harness noise but keep the actual sentence", () => {
  assert.equal(cleanReplaySummary("done\n\nI read the brief."), "I read the brief.");
  assert.equal(cleanReplaySummary("  Done ✓  wrote brief.md\n"), "wrote brief.md");
  assert.equal(cleanReplaySummary("completed: analysis ready"), "analysis ready");
  assert.equal(cleanReplaySummary("Donegal is not a completion marker"), "Donegal is not a completion marker");
  assert.equal(cleanReplaySummary("done"), "");
  assert.equal(cleanReplaySummary(null), "");
  assert.equal(cleanReplaySummary("alpha\u0000  beta"), "alpha beta");
  assert.equal(cleanReplaySummary("partly \uFFFD corrupted"), "partly corrupted");
  assert.equal(cleanReplaySummary("line one\nline two"), "line one line two");
});

test("step summary cleanup collapses whitespace only, never truncates silently", () => {
  const long = "word ".repeat(80).trim();
  assert.equal(cleanReplaySummary(long), long);
});
