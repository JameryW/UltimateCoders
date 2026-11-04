import assert from "node:assert/strict";
import { test } from "node:test";
import {
  HUMAN_TIER_TOOLS,
  REQUESTER_KINDS,
  approvalOrder,
  decideApproval,
  fetchApprovals,
  formatExpiry,
  isPending,
  isUsable,
  parseApproval,
  parseApprovals,
  requestApproval,
} from "../src/lib/approvalApi.ts";

const NOW_MS = Date.parse("2026-01-01T00:00:00Z");

function wire(overrides = {}) {
  return {
    id: "apr-1",
    tool: "scale_workers",
    case_id: "case-1",
    project_id: "proj-1",
    action_digest: "scale:worker=3",
    requested_by_kind: "agent",
    requested_by_id: "executor",
    reason: "queue is backing up",
    state: "pending",
    requested_at: "2026-01-01T00:00:00Z",
    expires_at: "2026-01-01T00:15:00Z",
    ...overrides,
  };
}

function parsed(overrides = {}) {
  const approval = parseApproval(wire(overrides));
  assert.ok(approval, "the fixture must parse");
  return approval;
}

/** Replace `fetch` for one test, restoring it afterwards; returns the calls. */
async function withFetch(handler, run) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return handler(String(url), init);
  };
  try {
    await run();
  } finally {
    globalThis.fetch = original;
  }
  return calls;
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return body;
    },
  };
}

// ── parse ─────────────────────────────────────────────────────

test("a well-formed approval is normalised to epoch millis", () => {
  const approval = parsed();
  assert.equal(approval.id, "apr-1");
  assert.equal(approval.tool, "scale_workers");
  assert.equal(approval.state, "pending");
  assert.equal(approval.requested_at, NOW_MS);
  assert.equal(approval.expires_at, NOW_MS + 15 * 60_000);
  assert.equal(approval.decided_at, undefined);
});

test("an unreadable record is dropped, not rendered as a broken row", () => {
  assert.equal(parseApproval(null), null);
  assert.equal(parseApproval({}), null);
  assert.equal(parseApproval(wire({ tool: "" })), null);
  assert.equal(parseApproval(wire({ state: "maybe" })), null);
  assert.deepEqual(parseApprovals([wire(), null, { id: "x" }]).length, 1);
  assert.deepEqual(parseApprovals("not a list"), []);
});

test("missing requester fields fall back to `unknown` instead of undefined", () => {
  const approval = parsed({ requested_by_kind: undefined, requested_by_id: undefined });
  assert.equal(approval.requested_by_kind, "unknown");
  assert.equal(approval.requested_by_id, "unknown");
});

// ── predicates ────────────────────────────────────────────────

test("pending needs a human; only a live approved permit is usable", () => {
  assert.equal(isPending(parsed()), true);
  assert.equal(isUsable(parsed(), NOW_MS), false);

  const approved = parsed({ state: "approved", decided_at: "2026-01-01T00:05:00Z" });
  assert.equal(isPending(approved), false);
  assert.equal(isUsable(approved, NOW_MS), true);
  assert.equal(isUsable(approved, NOW_MS + 20 * 60_000), false, "lapsed after expiry");
});

// ── order ─────────────────────────────────────────────────────

test("the queue is pending-first, then soonest expiry, then id", () => {
  const order = approvalOrder([
    parsed({ id: "used", state: "used" }),
    parsed({ id: "late", expires_at: "2026-01-01T01:00:00Z" }),
    parsed({ id: "soon", expires_at: "2026-01-01T00:05:00Z" }),
    parsed({ id: "mid", expires_at: "2026-01-01T00:30:00Z" }),
  ]);
  assert.deepEqual(
    order.map((entry) => entry.id),
    ["soon", "mid", "late", "used"],
  );
});

test("an unusable expiry sorts after real timestamps instead of first", () => {
  const order = approvalOrder([
    parsed({ id: "unknown", expires_at: 0 }),
    parsed({ id: "real", expires_at: "2026-01-01T00:05:00Z" }),
  ]);
  assert.deepEqual(
    order.map((entry) => entry.id),
    ["real", "unknown"],
  );
});

test("the input array is never mutated", () => {
  const input = [parsed({ id: "b" }), parsed({ id: "a" })];
  const before = input.map((entry) => entry.id);
  approvalOrder(input);
  assert.deepEqual(
    input.map((entry) => entry.id),
    before,
  );
});

// ── expiry text ───────────────────────────────────────────────

test("expiry text says how long the decision stays meaningful", () => {
  assert.equal(formatExpiry(parsed(), NOW_MS), "expires in 15 minutes");
  assert.equal(
    formatExpiry(parsed({ expires_at: "2025-12-31T23:59:00Z" }), NOW_MS),
    "expired, no decision",
  );
  assert.equal(
    formatExpiry(parsed({ state: "approved", expires_at: "2026-01-01T02:00:00Z" }), NOW_MS),
    "permit valid for 2 hours",
  );
  assert.equal(
    formatExpiry(parsed({ state: "approved", expires_at: "2025-12-31T23:00:00Z" }), NOW_MS),
    "permit lapsed",
  );
  assert.equal(formatExpiry(parsed({ state: "used" }), NOW_MS), "permit already used");
  assert.equal(formatExpiry(parsed({ state: "rejected" }), NOW_MS), "rejected");
  assert.equal(formatExpiry(parsed({ state: "expired" }), NOW_MS), "expired");
  assert.equal(formatExpiry(parsed({ expires_at: 0 }), NOW_MS), "expiry unknown");
});

// ── transport ─────────────────────────────────────────────────

test("fetchApprovals asks for the queue and normalises it", async () => {
  const calls = await withFetch(
    async () => jsonResponse(200, { approvals: [wire(), wire({ id: "apr-2", state: "used" })] }),
    async () => {
      const result = await fetchApprovals("pending", 10);
      assert.equal(result.ok, true);
      assert.deepEqual(
        result.data.map((entry) => entry.id),
        ["apr-1", "apr-2"],
      );
    },
  );
  assert.equal(calls[0].url, "/api/approvals?limit=10&state=pending");
});

test("a dead bridge is data, not an exception", async () => {
  await withFetch(
    async () => jsonResponse(503, { error: "governance_unavailable" }),
    async () => {
      const result = await fetchApprovals();
      assert.equal(result.ok, false);
      assert.equal(result.status, 503);
      assert.equal(result.error, "governance_unavailable");
    },
  );
});

test("a transport failure reports status 0 instead of throwing", async () => {
  await withFetch(
    async () => {
      throw new Error("connection refused");
    },
    async () => {
      const result = await fetchApprovals();
      assert.equal(result.ok, false);
      assert.equal(result.status, 0);
    },
  );
});

test("decideApproval posts the decision and trims the note", async () => {
  const calls = await withFetch(
    async () => jsonResponse(200, { approval: wire({ state: "approved" }) }),
    async () => {
      const result = await decideApproval("apr-1", true, "  ship it  ");
      assert.equal(result.ok, true);
      assert.equal(result.data.state, "approved");
    },
  );
  assert.equal(calls[0].url, "/api/approvals/apr-1/decide");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].init.body), { approve: true, note: "ship it" });
});

test("a blank note is not sent at all", async () => {
  const calls = await withFetch(
    async () => jsonResponse(200, { approval: wire({ state: "rejected" }) }),
    async () => {
      await decideApproval("apr-1", false, "   ");
    },
  );
  assert.deepEqual(JSON.parse(calls[0].init.body), { approve: false });
});

test("a token-gated deployment surfaces 401 instead of an empty queue", async () => {
  await withFetch(
    async () => jsonResponse(401, { error: "unauthorized" }),
    async () => {
      const result = await decideApproval("apr-1", true);
      assert.equal(result.ok, false);
      assert.equal(result.status, 401);
      assert.equal(result.error, "unauthorized");
    },
  );
});

test("a malformed decide reply is 502, not a silent success", async () => {
  await withFetch(
    async () => jsonResponse(200, { approval: null }),
    async () => {
      const result = await decideApproval("apr-1", true);
      assert.equal(result.ok, false);
      assert.equal(result.status, 502);
    },
  );
});

// ── requesting a permit ──────────────────────────────────────

test("requesting sends the agent identity and the optional context", async () => {
  const calls = await withFetch(
    async () => jsonResponse(200, { approval: wire() }),
    async () => {
      const result = await requestApproval({
        tool: "  scale_workers  ",
        caseId: "case-1",
        projectId: "proj-1",
        actionDigest: "scale:worker=3",
        reason: "queue is backing up",
        requestedByKind: "agent",
        requestedById: "executor",
        ttlMinutes: 5,
      });
      assert.equal(result.ok, true);
      assert.equal(result.data.tool, "scale_workers");
    },
  );
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/approvals");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    tool: "scale_workers",
    requested_by_kind: "agent",
    requested_by_id: "executor",
    case_id: "case-1",
    project_id: "proj-1",
    action_digest: "scale:worker=3",
    reason: "queue is backing up",
    ttl_minutes: 5,
  });
});

test("blank optional fields are omitted, not sent as empty strings", async () => {
  const calls = await withFetch(
    async () => jsonResponse(200, { approval: wire() }),
    async () => {
      const result = await requestApproval({ tool: "docker_run" });
      assert.equal(result.ok, true);
    },
  );
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    tool: "docker_run",
    requested_by_kind: "agent",
    requested_by_id: "uc-worker",
  });
});

test("a human cannot request as themselves", async () => {
  // The gateway refuses a `user` requester; the client must not even send it.
  const calls = await withFetch(
    async () => jsonResponse(200, { approval: wire() }),
    async () => {
      const result = await requestApproval({
        tool: "scale_workers",
        requestedByKind: "user",
      });
      assert.equal(result.ok, false);
      assert.equal(result.status, 400);
    },
  );
  assert.equal(calls.length, 0);
  assert.deepEqual([...REQUESTER_KINDS], ["agent", "service"]);
  assert.ok(!REQUESTER_KINDS.includes("user"));
});

test("an empty tool or a non-positive ttl is refused before the wire", async () => {
  const calls = await withFetch(
    async () => jsonResponse(200, { approval: wire() }),
    async () => {
      assert.equal((await requestApproval({ tool: "   " })).ok, false);
      assert.equal((await requestApproval({ tool: "docker_run", ttlMinutes: 0 })).ok, false);
      assert.equal((await requestApproval({ tool: "docker_run", ttlMinutes: -3 })).ok, false);
    },
  );
  assert.equal(calls.length, 0);
});

test("the human-tier tool list is the two the gateway gates", async () => {
  assert.deepEqual([...HUMAN_TIER_TOOLS], ["scale_workers", "docker_run"]);
});

test("a rejected request surfaces the gateway status instead of pretending", async () => {
  await withFetch(
    async () => jsonResponse(403, { error: "missing_permission" }),
    async () => {
      const result = await requestApproval({ tool: "scale_workers" });
      assert.equal(result.ok, false);
      assert.equal(result.status, 403);
    },
  );
  await withFetch(
    async () => jsonResponse(401, { error: "unauthorized" }),
    async () => {
      assert.equal((await requestApproval({ tool: "scale_workers" })).status, 401);
    },
  );
  await withFetch(
    async () => jsonResponse(503, { error: "governance_unavailable" }),
    async () => {
      assert.equal((await requestApproval({ tool: "scale_workers" })).status, 503);
    },
  );
});

test("a malformed request reply is 502", async () => {
  await withFetch(
    async () => jsonResponse(200, { approval: { id: 42 } }),
    async () => {
      const result = await requestApproval({ tool: "scale_workers" });
      assert.equal(result.ok, false);
      assert.equal(result.status, 502);
    },
  );
});
