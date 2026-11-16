import assert from "node:assert/strict";
import { test } from "node:test";

import {
  FAILURE_MODES,
  FAILURE_PHASES,
  buildFailureHeatmap,
  classifyPtaQuality,
} from "../src/lib/failureHeatmap.ts";

function cellFor(heatmap, modeId) {
  const cell = heatmap.cells.find((entry) => entry.modeId === modeId);
  assert.ok(cell, `missing cell for ${modeId}`);
  return cell;
}

test("the catalogue exposes the seven v10 phases in order", () => {
  assert.deepEqual(FAILURE_PHASES, [
    "planning",
    "retrieval",
    "editing",
    "tooling",
    "testing",
    "review",
    "delivery",
  ]);
});

test("the catalogue has 24 unique modes covering every phase", () => {
  assert.equal(FAILURE_MODES.length, 24);
  const ids = FAILURE_MODES.map((mode) => mode.id);
  assert.equal(new Set(ids).size, 24);
  for (const mode of FAILURE_MODES) {
    assert.equal(typeof mode.label, "string");
    assert.ok(mode.label.length > 0);
    assert.ok(FAILURE_PHASES.includes(mode.phase));
    assert.ok(Number.isInteger(mode.severity));
    assert.ok(mode.severity >= 1 && mode.severity <= 5);
    assert.equal(typeof mode.deterministic, "boolean");
  }
  for (const phase of FAILURE_PHASES) {
    assert.ok(
      FAILURE_MODES.some((mode) => mode.phase === phase),
      `phase ${phase} has no modes`,
    );
  }
});

test("exactly 18 of the 24 failure modes are deterministic", () => {
  assert.equal(FAILURE_MODES.filter((mode) => mode.deterministic).length, 18);
});

test("buildFailureHeatmap aggregates counts and derives rates", () => {
  const heatmap = buildFailureHeatmap([
    { type: "failure", phase: "testing", mode: "test_not_run" },
    { type: "failure", phase: "testing", mode: "test_not_run" },
    { type: "failure", phase: "editing", mode: "edit_wrong_file" },
    { type: "failure", phase: "delivery", mode: "delivery_broken_artifact" },
    { type: "failure", phase: "review", mode: "review_false_approve" },
  ]);

  assert.equal(heatmap.phases.length, 7);
  assert.equal(heatmap.modes.length, 24);
  assert.equal(heatmap.cells.length, 24);
  assert.equal(heatmap.totalEvents, 5);

  const testNotRun = cellFor(heatmap, "test_not_run");
  assert.equal(testNotRun.count, 2);
  assert.equal(testNotRun.rate, 0.4);
  assert.equal(testNotRun.phase, "testing");
  assert.equal(testNotRun.deterministic, true);

  const editWrongFile = cellFor(heatmap, "edit_wrong_file");
  assert.equal(editWrongFile.count, 1);
  assert.equal(editWrongFile.rate, 0.2);

  // Four of the five counted events are deterministic modes.
  assert.equal(heatmap.deterministicRate, 0.8);

  const counted = heatmap.cells.reduce((sum, cell) => sum + cell.count, 0);
  assert.equal(counted, heatmap.totalEvents);
  for (const cell of heatmap.cells) {
    assert.ok(cell.rate >= 0 && cell.rate <= 1);
    const mode = FAILURE_MODES.find((entry) => entry.id === cell.modeId);
    assert.ok(mode);
    assert.equal(cell.deterministic, mode.deterministic);
    assert.equal(cell.phase, mode.phase);
  }
});

test("unknown and malformed modes are ignored rather than invented", () => {
  const heatmap = buildFailureHeatmap([
    { type: "failure", mode: "not_a_real_mode" },
    { type: "failure", mode: "" },
    { type: "failure" },
    null,
    "not an event",
    { type: "failure", phase: "testing", mode: "test_not_run" },
  ]);

  assert.equal(heatmap.totalEvents, 1);
  assert.equal(cellFor(heatmap, "test_not_run").count, 1);
  assert.equal(heatmap.cells.some((cell) => cell.modeId === "not_a_real_mode"), false);
  assert.equal(heatmap.cells.length, 24);
});

test("an empty stream yields bounded zero rates", () => {
  const heatmap = buildFailureHeatmap([]);
  assert.equal(heatmap.totalEvents, 0);
  assert.equal(heatmap.deterministicRate, 0);
  assert.equal(heatmap.cells.every((cell) => cell.count === 0 && cell.rate === 0), true);
});

test("event and cell counts stay bounded by maxEvents/maxCount", () => {
  const repeated = Array.from({ length: 10 }, () => ({
    type: "failure",
    phase: "editing",
    mode: "edit_wrong_file",
  }));

  const cappedCell = buildFailureHeatmap(repeated, { maxCount: 3 });
  assert.equal(cellFor(cappedCell, "edit_wrong_file").count, 3);
  assert.equal(cappedCell.totalEvents, 3);

  const cappedWindow = buildFailureHeatmap(repeated, { maxEvents: 2 });
  assert.equal(cellFor(cappedWindow, "edit_wrong_file").count, 2);
  assert.equal(cappedWindow.totalEvents, 2);

  const emptyWindow = buildFailureHeatmap(repeated, { maxEvents: 0 });
  assert.equal(emptyWindow.totalEvents, 0);
  assert.equal(emptyWindow.deterministicRate, 0);
});

test("rates round to four decimal places", () => {
  const heatmap = buildFailureHeatmap([
    { type: "failure", mode: "test_not_run" },
    { type: "failure", mode: "edit_wrong_file" },
    { type: "failure", mode: "edit_wrong_file" },
  ]);
  assert.equal(cellFor(heatmap, "test_not_run").rate, 0.3333);
  assert.equal(cellFor(heatmap, "edit_wrong_file").rate, 0.6667);
  assert.equal(heatmap.deterministicRate, 1);
});

test("classifyPtaQuality grades lucky, solid and ideal passes", () => {
  assert.equal(classifyPtaQuality(), "solid");
  assert.equal(classifyPtaQuality({ luckyPass: true }), "lucky");
  assert.equal(
    classifyPtaQuality({ luckyPass: true, testEvidence: true, processClean: true, retries: 0 }),
    "lucky",
  );
  assert.equal(classifyPtaQuality({ testEvidence: false }), "lucky");
  assert.equal(classifyPtaQuality({ processClean: false, retries: 2 }), "lucky");
  assert.equal(classifyPtaQuality({ processClean: false, retries: 1 }), "solid");
  assert.equal(
    classifyPtaQuality({ testEvidence: true, processClean: true, retries: 0 }),
    "ideal",
  );
  assert.equal(
    classifyPtaQuality({ testEvidence: true, processClean: true }),
    "ideal",
  );
  assert.equal(
    classifyPtaQuality({ testEvidence: true, processClean: true, retries: 1 }),
    "solid",
  );
  assert.equal(
    classifyPtaQuality({ luckyPass: false, testEvidence: true, processClean: false }),
    "solid",
  );
  assert.equal(
    classifyPtaQuality({ testEvidence: true, processClean: true, retries: -3 }),
    "ideal",
  );
});
