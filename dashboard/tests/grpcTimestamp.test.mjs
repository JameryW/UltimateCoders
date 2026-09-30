import assert from "node:assert/strict";
import { test } from "node:test";
import { eventTimestampToISO, taskTimestampToISO } from "../src/lib/grpcTimestamp.ts";

test("a real Gateway task timestamp renders its 2026 creation time", () => {
  assert.equal(taskTimestampToISO(1790756322n), "2026-09-30T08:18:42.000Z");
  assert.equal(taskTimestampToISO(0n), "1970-01-01T00:00:00.000Z");
});

test("event timestamps normalize every observed transport epoch unit", () => {
  const iso = "2026-09-30T08:18:42.000Z";
  for (const value of [1790756322, "1790756322", 1790756322000n, "1790756322000000", iso]) {
    assert.equal(eventTimestampToISO(value), iso);
  }
});
