import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DASHBOARD_HREF,
  HOME_DOCUMENT_TITLE,
  RUNTIME_MAX_FAILURES,
  RUNTIME_POLL_INTERVAL_MS,
  RUNTIME_RETRY_BASE_MS,
  RUNTIME_RETRY_MAX_MS,
  WORKFLOW_HREF,
  classifyProbeResponse,
  formatSyncAge,
  nextPollDelay,
  runtimeBadge,
  runtimeStatusLabel,
  showcasePrimaryCta,
  showcaseRuntimeMode,
  shouldStopPolling,
  transportLabel,
} from "../src/lib/homeRuntime.ts";

test("only a 2xx JSON answer proves the Gateway is behind this host", () => {
  // The showcase may be published on a static host, where a SPA fallback answers
  // /dashboard/api/health with 200 text/html and a missing upstream answers 502.
  assert.equal(classifyProbeResponse(200, "application/json"), "reachable");
  assert.equal(classifyProbeResponse(200, "application/json; charset=utf-8"), "reachable");
  assert.equal(classifyProbeResponse(204, "application/json"), "reachable");
  assert.equal(classifyProbeResponse(200, "text/html"), "absent");
  assert.equal(classifyProbeResponse(200, null), "absent");
  assert.equal(classifyProbeResponse(502, "text/html"), "absent");
  assert.equal(classifyProbeResponse(401, "application/json"), "absent");
  assert.equal(classifyProbeResponse(0, null), "absent");
});

test("the runtime card can only claim LIVE when the transport is up and nothing fails", () => {
  const cases = [
    // probe, connected, failures, mode
    ["probing", false, 0, "checking"],
    ["probing", true, 0, "checking"],
    ["absent", false, 0, "preview"],
    ["absent", true, 0, "preview"],
    ["reachable", true, 0, "live"],
    ["reachable", false, 0, "connecting"],
    ["reachable", true, 1, "connecting"],
    ["reachable", true, RUNTIME_MAX_FAILURES, "connecting"],
  ];
  for (const [probe, connected, consecutiveFailures, expected] of cases) {
    assert.equal(
      showcaseRuntimeMode({ probe, connected, consecutiveFailures }),
      expected,
      `probe=${probe} connected=${connected} failures=${consecutiveFailures}`,
    );
  }
});

test("LIVE is unreachable without a connected transport", () => {
  for (const probe of ["probing", "absent", "reachable"]) {
    for (let failures = 0; failures <= RUNTIME_MAX_FAILURES + 1; failures += 1) {
      const mode = showcaseRuntimeMode({ probe, connected: false, consecutiveFailures: failures });
      assert.notEqual(mode, "live", `probe=${probe} failures=${failures}`);
    }
  }
});

test("every runtime mode has a distinct badge and a non-empty status label", () => {
  const modes = ["checking", "connecting", "live", "preview"];
  const badges = modes.map((mode) => runtimeBadge(mode));
  const labels = modes.map((mode) => runtimeStatusLabel(mode));
  assert.equal(new Set(badges).size, modes.length, "badges must be distinguishable");
  for (const badge of badges) assert.equal(typeof badge, "string");
  for (const label of labels) assert.ok(label.length > 4, `label too short: ${label}`);
  // A preview must not claim a connection.
  assert.match(runtimeStatusLabel("preview"), /preview/i);
});

test("every transport state has a label", () => {
  for (const state of ["connected", "connecting", "reconnecting", "error", "disconnected"]) {
    assert.match(transportLabel(state), /Gateway/);
  }
});

test("poll cadence backs off on failures and stops instead of retrying forever", () => {
  assert.equal(nextPollDelay(0), RUNTIME_POLL_INTERVAL_MS);
  assert.equal(nextPollDelay(-1), RUNTIME_POLL_INTERVAL_MS);
  assert.equal(nextPollDelay(1), RUNTIME_RETRY_BASE_MS);
  assert.equal(nextPollDelay(2), RUNTIME_RETRY_BASE_MS * 2);
  let previous = 0;
  for (let failures = 1; failures <= 12; failures += 1) {
    const delay = nextPollDelay(failures);
    assert.ok(delay > 0, `delay must stay positive at ${failures}`);
    assert.ok(delay >= previous, `delay must not shrink at ${failures}`);
    assert.ok(delay <= RUNTIME_RETRY_MAX_MS, `delay must respect the ceiling at ${failures}`);
    previous = delay;
  }
  assert.equal(nextPollDelay(12), RUNTIME_RETRY_MAX_MS);
});

test("polling stops at the failure threshold, never before it", () => {
  for (let failures = 0; failures < RUNTIME_MAX_FAILURES; failures += 1) {
    assert.equal(shouldStopPolling(failures), false, `must keep polling at ${failures}`);
  }
  for (const failures of [RUNTIME_MAX_FAILURES, RUNTIME_MAX_FAILURES + 1, 50]) {
    assert.equal(shouldStopPolling(failures), true, `must stop at ${failures}`);
  }
});

test("sync age is human readable and survives a backwards clock", () => {
  const now = 1_700_000_000_000;
  assert.equal(formatSyncAge(null, now), "waiting for first sync");
  assert.equal(formatSyncAge(now, now), "just now");
  assert.equal(formatSyncAge(now - 3_000, now), "just now");
  assert.equal(formatSyncAge(now + 5_000, now), "just now", "future timestamp clamps");
  assert.equal(formatSyncAge(now - 12_000, now), "12s ago");
  assert.equal(formatSyncAge(now - 59_000, now), "59s ago");
  assert.equal(formatSyncAge(now - 65_000, now), "1m ago");
  assert.equal(formatSyncAge(now - 3_700_000, now), "1h ago");
});

test("the overview owns the document title while it is mounted", () => {
  assert.match(HOME_DOCUMENT_TITLE, /UltimateCoders/);
  assert.notEqual(HOME_DOCUMENT_TITLE, "UltimateCoders Dashboard", "that title names the operations shell");
});

test("the primary action never promises a dashboard this host cannot serve", () => {
  // Only a reachable Gateway (probe `reachable` -> live/connecting) may advertise
  // /dashboard: on the static showcase that route renders a shell pinned at
  // OFFLINE, so the CTA sends the visitor to the execution loop instead.
  for (const mode of ["live", "connecting"]) {
    const cta = showcasePrimaryCta(mode);
    assert.equal(cta.href, DASHBOARD_HREF, `${mode} must open the dashboard`);
    assert.match(cta.label, /dashboard/i, `${mode} label must name the dashboard`);
    assert.ok(cta.note.length > 0, `${mode} still needs a one-line note`);
    assert.doesNotMatch(cta.note, /No Gateway/, `${mode} has a Gateway and must not claim otherwise`);
  }
  for (const mode of ["checking", "preview"]) {
    const cta = showcasePrimaryCta(mode);
    assert.equal(cta.href, WORKFLOW_HREF, `${mode} must not send the visitor to a dead shell`);
    assert.notEqual(cta.label, "", `${mode} needs a label`);
    assert.match(cta.note, /Gateway/, `${mode} must say why there is no dashboard`);
    assert.match(cta.note, /deployment/i, `${mode} must point at the deployment that does have one`);
  }
  const preview = showcasePrimaryCta("preview");
  assert.notEqual(preview.label, "Open operations dashboard");
  assert.notEqual(preview.href, DASHBOARD_HREF);
});

