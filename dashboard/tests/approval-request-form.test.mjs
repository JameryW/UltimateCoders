import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const dashboardRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

/**
 * The queue's request form is the UI half of `approval.request`.
 *
 * Two contracts are pinned by rendering it:
 *
 * 1. the form exists at all — the op had no caller before, so "it renders" is the
 *    difference between a reachable loop and a dead endpoint;
 * 2. the requester kind offers `agent` and `service` only. A `user` option would
 *    be a human asking themselves for permission, which the gateway refuses.
 */
test("the approval panel renders a request form constrained to agent/service", async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const { ApprovalPanel } = await vite.ssrLoadModule(
      "/src/components/panels/ApprovalPanel.tsx",
    );
    const html = renderToStaticMarkup(React.createElement(ApprovalPanel));

    assert.match(html, /data-testid="approval-request-form"/);
    assert.match(html, /id="approval-request-tool"/);
    assert.match(html, /id="approval-request-by"/);
    assert.match(html, /id="approval-request-kind"/);
    assert.match(html, /申请/);

    // The human-tier tools are offered as suggestions.
    assert.match(html, /<datalist id="approval-human-tier-tools"/);
    assert.match(html, /value="scale_workers"/);
    assert.match(html, /value="docker_run"/);

    // Requester kinds: agent + service, never user.
    assert.match(html, /<option value="agent"[^>]*>agent<\/option>/);
    assert.match(html, /<option value="service"[^>]*>service<\/option>/);
    assert.doesNotMatch(html, /<option value="user"[^>]*>/);
  } finally {
    await vite.close();
  }
});
