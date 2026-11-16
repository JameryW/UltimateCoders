import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DashboardEventsRequestError,
  fetchDashboardEvents,
  mergeDashboardEvents,
  normaliseRestDashboardEvent,
} from "../src/lib/dashboardEvents.ts";

function event(overrides = {}) {
  return { timestamp: "2026-11-16T10:00:00Z", type: "subtask_failed", ...overrides };
}

test("normalises the durable REST data field into dashboard details", () => {
  const normalised = normaliseRestDashboardEvent(
    event({ data: { task_id: "t1", subtask_id: "s1", error: "timeout" } }),
  );
  assert.deepEqual(normalised, {
    timestamp: "2026-11-16T10:00:00Z",
    type: "subtask_failed",
    details: { task_id: "t1", subtask_id: "s1", error: "timeout" },
  });
});

test("normalises an already-mapped dashboard details field", () => {
  const normalised = normaliseRestDashboardEvent(event({ details: { reason: "max turns" } }));
  assert.equal(normalised?.details.reason, "max turns");
});

test("drops malformed REST events instead of inventing values", () => {
  assert.equal(normaliseRestDashboardEvent(null), null);
  assert.equal(normaliseRestDashboardEvent({ type: "subtask_failed" }), null);
  assert.equal(normaliseRestDashboardEvent({ timestamp: "2026-11-16T10:00:00Z" }), null);
  assert.equal(normaliseRestDashboardEvent([]), null);
});

test("fetchDashboardEvents sends the bearer token and normalises the payload", async () => {
  const calls = [];
  const fakeFetch = async (url, init) => {
    calls.push({ url, init });
    return {
      ok: true,
      async json() {
        return {
          available: true,
          events: [event({ data: { task_id: "t1", error: "timed out" } }), { type: "bad" }],
        };
      },
    };
  };

  const events = await fetchDashboardEvents(fakeFetch, "secret", undefined);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/dashboard/api/events?limit=500");
  assert.equal(calls[0].init.headers.Authorization, "Bearer secret");
  assert.equal(events.length, 1);
  assert.equal(events[0].details.error, "timed out");
});

test("fetchDashboardEvents rejects non-OK responses with a typed error", async () => {
  const fakeFetch = async () => ({ ok: false, status: 401 });
  await assert.rejects(fetchDashboardEvents(fakeFetch), (error) => {
    assert.ok(error instanceof DashboardEventsRequestError);
    assert.equal(error.status, 401);
    return true;
  });
});

test("fetchDashboardEvents rejects malformed payloads", async () => {
  const fakeFetch = async () => ({ ok: true, async json() { return { events: "no" }; } });
  await assert.rejects(fetchDashboardEvents(fakeFetch), /malformed/);
});

test("mergeDashboardEvents keeps history and live events without duplicating a transition", () => {
  const first = event({ details: { task_id: "t1", subtask_id: "s1" } });
  const second = event({ timestamp: "2026-11-16T10:00:01Z", details: { task_id: "t1", subtask_id: "s2" } });
  const merged = mergeDashboardEvents([first], [first, second]);
  assert.deepEqual(merged, [first, second]);
});
