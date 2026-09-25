import assert from "node:assert/strict";
import { test } from "node:test";
import { latestDashboardEvents } from "../src/lib/dashboardEvents.ts";

test("cluster rail shows the newest events from the dashboard API", () => {
  const events = Array.from({ length: 8 }, (_, i) => ({
    timestamp: `2026-09-24T22:00:0${8 - i}+08:00`,
    type: `event-${8 - i}`,
    details: {},
  }));

  assert.deepEqual(
    latestDashboardEvents(events).map((event) => event.type),
    ["event-8", "event-7", "event-6", "event-5", "event-4"],
  );
});
