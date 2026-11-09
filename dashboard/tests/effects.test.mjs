import assert from "node:assert/strict";
import { test } from "node:test";
import {
  EFFECT_STALE_STATUS,
  EFFECT_STATUSES,
  appendEffectPage,
  effectAge,
  effectCountLine,
  effectHeadline,
  effectOwnerLine,
  effectRequestLine,
  effectsFromPayload,
  effectsQuery,
} from "../src/lib/effects.ts";

function receipt(overrides = {}) {
  return {
    effect_id: "eff_a",
    kind: "push-branch",
    status: "UNKNOWN",
    owner: "",
    lease_until: 0,
    lease_expired: false,
    updated_at: 1_000,
    reason: "lost response",
    request: { branch: "uc/subtask/abc" },
    ...overrides,
  };
}

test("the queue query names one status and carries the cursor", () => {
  assert.equal(effectsQuery(), "/dashboard/api/effects?status=UNKNOWN&limit=100");
  assert.equal(
    effectsQuery({ status: "RECONCILING", stale: true, limit: 5 }),
    "/dashboard/api/effects?status=RECONCILING&limit=5&stale=1",
  );
  assert.equal(
    effectsQuery({ status: "RECONCILING", stale: true, after: "eff z" }),
    "/dashboard/api/effects?status=RECONCILING&limit=100&stale=1&after=eff%20z",
  );
  assert.ok(!effectsQuery({ after: "eff_a" }).includes("stale=1"));
  assert.ok(EFFECT_STATUSES.includes("UNKNOWN"));
  assert.equal(EFFECT_STALE_STATUS, "RECONCILING");
});

test("ages are short and never negative", () => {
  assert.equal(effectAge(1_000, 1_020), "20s");
  assert.equal(effectAge(1_000, 1_600), "10m");
  assert.equal(effectAge(1_000, 1_000 + 3 * 3600), "3h");
  assert.equal(effectAge(1_000, 1_000 + 2 * 86400), "2d");
  assert.equal(effectAge(2_000, 1_000), "0s");
  assert.equal(effectAge(0), "");
});

test("the owner line says when a lease has expired", () => {
  assert.equal(
    effectOwnerLine(receipt({ owner: "ops:a", lease_expired: true })),
    "ops:a · lease expired",
  );
  assert.equal(effectOwnerLine(receipt({ owner: "ops:a", lease_until: 0 })), "ops:a");
  assert.equal(effectOwnerLine(receipt()), "");
  const live = effectOwnerLine(
    receipt({ owner: "ops:a", lease_until: Date.now() / 1000 + 30 }),
  );
  assert.match(live, /^ops:a · lease \d+s$/);
});

test("the scope line names ids the operator already knows", () => {
  assert.equal(effectRequestLine({ branch: "uc/subtask/abc" }), "branch uc/subtask/abc");
  assert.equal(
    effectRequestLine({ operation_id: "exp-1:2", backend_id: "b1" }),
    "operation exp-1:2 · backend b1",
  );
  assert.equal(
    effectRequestLine({ branch: "main", merged_branches: ["uc/subtask/a", "uc/subtask/b"] }),
    "branch main · 2 merged branch(es)",
  );
  assert.equal(effectRequestLine({}), "");
  assert.equal(effectRequestLine(undefined), "");
});

test("a headline carries the state and how long it has been stuck", () => {
  assert.match(effectHeadline(receipt(), 1_060), /^UNKNOWN · 1m$/);
  assert.equal(effectHeadline(receipt({ updated_at: 0 })), "UNKNOWN");
});

test("the payload keeps the endpoint's counts and invents none", () => {
  const queue = effectsFromPayload({
    effects: [receipt()],
    count: 1,
    total: 7,
    next_after: "eff_a",
  });
  assert.equal(queue.rows.length, 1);
  assert.equal(queue.total, 7);
  assert.equal(queue.nextAfter, "eff_a");

  const empty = effectsFromPayload({});
  assert.deepEqual(empty.rows, []);
  assert.equal(empty.total, 0);
  assert.equal(empty.nextAfter, null);
});

test("paging appends without repeating a receipt", () => {
  const first = appendEffectPage(
    { rows: [], total: 0, nextAfter: null },
    { effects: [receipt(), receipt({ effect_id: "eff_b" })], total: 3, next_after: "eff_b" },
  );
  assert.deepEqual(
    first.rows.map((row) => row.effect_id),
    ["eff_a", "eff_b"],
  );

  const second = appendEffectPage(first, {
    effects: [receipt({ effect_id: "eff_b" }), receipt({ effect_id: "eff_c" })],
    total: 3,
    next_after: null,
  });
  assert.deepEqual(
    second.rows.map((row) => row.effect_id),
    ["eff_a", "eff_b", "eff_c"],
  );
  assert.equal(second.nextAfter, null);
});

test("the count line never claims the window is the whole queue", () => {
  assert.equal(effectCountLine(2, 7, "UNKNOWN"), "2 of 7 UNKNOWN");
  assert.equal(effectCountLine(0, 0, "UNKNOWN"), "0 of 0 UNKNOWN");
});

async function renderList(props) {
  const { createServer } = await import("vite");
  const React = (await import("react")).default;
  const { renderToStaticMarkup } = await import("react-dom/server");
  const vite = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
    optimizeDeps: { noDiscovery: true },
  });
  try {
    const { EffectQueueList } = await vite.ssrLoadModule(
      "/src/components/panels/EffectQueuePanel.tsx",
    );
    return renderToStaticMarkup(React.createElement(EffectQueueList, props));
  } finally {
    await vite.close();
  }
}

test("an operator sees the state, who holds it, and what it was recorded against", async () => {
  const html = await renderList({
    rows: [
      receipt({
        kind: "metainfer-kill",
        status: "RECONCILING",
        owner: "ops:a",
        lease_expired: true,
        reason: "lease expired",
        request: { operation_id: "exp-1:2", backend_id: "b1" },
      }),
    ],
  });
  assert.match(html, /metainfer-kill · RECONCILING/);
  assert.match(html, /ops:a · lease expired/);
  assert.match(html, /operation exp-1:2 · backend b1/);
});

test("an empty queue says so instead of pretending", async () => {
  const html = await renderList({ rows: [] });
  assert.match(html, /No receipts in this state\./);
});
