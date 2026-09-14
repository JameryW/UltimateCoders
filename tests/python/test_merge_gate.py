"""T9 #651 / D9 #646 — merge barrier: cross-language golden + gate flow.

Covers:
- The merge idempotency key derivation mirrored from Rust
  ``uc_types::merge::derive_merge_idempotency_key`` (golden pinned on both
  sides — if either changes the preimage format or truncation, both fail).
- Orchestrator ``_arbitrate_task`` gate flow: refuse → skip; replay → skip
  + no-op report; fresh grant → arbitrate then report with the key.
- The ``MergeGate`` adapter in nats_worker (engine call mapping).
"""

from __future__ import annotations

import asyncio
import hashlib

from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.nats_worker import MergeGate

# ── Cross-language golden ───────────────────────────────────────

MERGE_KEY_LEN = 32

RUST_GOLDEN_KEY = "70944265bd1ad20766b7bfa2f7d5fc4b"


def derive_merge_idempotency_key(
    graph_id: str, succeeded: list[tuple[str, str]],
) -> str:
    """Python mirror of uc_types::merge::derive_merge_idempotency_key.

    Preimage: ``merge:{graph_id}:{SUCCEEDED ids CSV}:{node=sha pairs}`` —
    nodes sorted by node_id (byte order), CSV joins ids with ``,`` and the
    pairs section joins ``{node_id}={sha256(output)}`` with ``;``.
    Truncated to 32 hex chars.
    """
    ordered = sorted(succeeded, key=lambda pair: pair[0])
    csv = ",".join(node_id for node_id, _ in ordered)
    pairs = ";".join(f"{node_id}={sha}" for node_id, sha in ordered)
    preimage = f"merge:{graph_id}:{csv}:{pairs}"
    return hashlib.sha256(preimage.encode()).hexdigest()[:MERGE_KEY_LEN]


def _golden_input() -> list[tuple[str, str]]:
    return [
        ("n-1", hashlib.sha256(b"out-1").hexdigest()),
        ("n-2", hashlib.sha256(b"out-2").hexdigest()),
    ]


def test_merge_key_matches_rust_golden():
    assert derive_merge_idempotency_key("g-1", _golden_input()) == RUST_GOLDEN_KEY


def test_merge_key_is_order_insensitive_over_succeeded_set():
    flipped = list(reversed(_golden_input()))
    assert derive_merge_idempotency_key("g-1", flipped) == (
        derive_merge_idempotency_key("g-1", _golden_input())
    )


def test_merge_key_differs_per_graph_node_and_output():
    base = derive_merge_idempotency_key("g-1", _golden_input())
    # Different graph.
    assert base != derive_merge_idempotency_key("g-2", _golden_input())
    # Different SUCCEEDED set (stale aggregation loses).
    fewer = _golden_input()[:1]
    assert base != derive_merge_idempotency_key("g-1", fewer)
    # Different output bytes (same node set).
    other = [("n-1", hashlib.sha256(b"out-X").hexdigest())]
    assert derive_merge_idempotency_key("g-1", fewer) != (
        derive_merge_idempotency_key("g-1", other)
    )


def test_merge_key_handles_empty_succeeded_set():
    k = derive_merge_idempotency_key("g-1", [])
    assert len(k) == MERGE_KEY_LEN
    assert k == derive_merge_idempotency_key("g-1", [])


# ── Orchestrator gate flow ──────────────────────────────────────


class FakeArbiter:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def arbitrate(self, branches: list[str]) -> dict:
        self.calls.append(list(branches))
        return {
            "status": "merged",
            "merged_branches": list(branches),
            "conflict_branches": [],
            "push_status": "pushed",
        }


class FakeGate:
    def __init__(self, decision: dict, report: dict | None = None) -> None:
        self.decision = decision
        self.report_response = report or {
            "accepted": True,
            "idempotent_replay": False,
        }
        self.grant_calls: list[str] = []
        self.reports: list[tuple[str, str, dict]] = []

    async def issue_merge_grant(self, graph_id: str) -> dict:
        self.grant_calls.append(graph_id)
        return dict(self.decision)

    async def report_merge_outcome(
        self, graph_id: str, merge_idempotency_key: str, outcome: dict,
    ) -> dict:
        self.reports.append((graph_id, merge_idempotency_key, outcome))
        return dict(self.report_response)


BRANCHES = ["uc/subtask/aaaaaaaaaaaa", "uc/subtask/bbbbbbbbbbbb"]


def test_grant_refused_skips_arbitration_and_report():
    arbiter = FakeArbiter()
    gate = FakeGate({"granted": False, "merge_idempotency_key": "",
                     "idempotent_replay": False, "error": "not quiescent"})
    orch = Orchestrator(merge_arbiter=arbiter, merge_gate=gate)
    asyncio.run(orch._arbitrate_task("t-1", BRANCHES))
    assert gate.grant_calls == ["t-1"]
    assert arbiter.calls == []  # never merge unauthorized
    assert gate.reports == []


def test_fresh_grant_arbitrates_then_reports_with_key():
    arbiter = FakeArbiter()
    gate = FakeGate({"granted": True,
                     "merge_idempotency_key": "k" * 32,
                     "idempotent_replay": False, "error": ""})
    orch = Orchestrator(merge_arbiter=arbiter, merge_gate=gate)
    asyncio.run(orch._arbitrate_task("t-1", BRANCHES))
    assert arbiter.calls == [BRANCHES]
    assert len(gate.reports) == 1
    graph_id, key, outcome = gate.reports[0]
    assert graph_id == "t-1"
    assert key == "k" * 32
    assert outcome == {
        "status": "merged",
        "merged_branches": BRANCHES,
        "conflict_branches": [],
        "push_status": "pushed",
    }


def test_idempotent_replay_skips_arbitration_and_report():
    arbiter = FakeArbiter()
    gate = FakeGate({"granted": True,
                     "merge_idempotency_key": "k" * 32,
                     "idempotent_replay": True, "error": ""})
    orch = Orchestrator(merge_arbiter=arbiter, merge_gate=gate)
    asyncio.run(orch._arbitrate_task("t-1", BRANCHES))
    # The merge already reported on the first pass — skip execution AND
    # the (no-op) report.
    assert gate.grant_calls == ["t-1"]
    assert arbiter.calls == []
    assert gate.reports == []


def test_rejected_report_is_non_fatal():
    arbiter = FakeArbiter()
    gate = FakeGate(
        {"granted": True, "merge_idempotency_key": "k" * 32,
         "idempotent_replay": False, "error": ""},
        report={"accepted": False, "idempotent_replay": False},
    )
    orch = Orchestrator(merge_arbiter=arbiter, merge_gate=gate)
    # Must not raise — a rejected report is logged loudly, never fatal.
    asyncio.run(orch._arbitrate_task("t-1", BRANCHES))
    assert arbiter.calls == [BRANCHES]
    assert len(gate.reports) == 1


def test_no_gate_preserves_legacy_arbitration():
    arbiter = FakeArbiter()
    orch = Orchestrator(merge_arbiter=arbiter)
    asyncio.run(orch._arbitrate_task("t-1", BRANCHES))
    assert arbiter.calls == [BRANCHES]


def test_no_arbiter_is_noop_even_with_gate():
    gate = FakeGate({"granted": True,
                     "merge_idempotency_key": "k" * 32,
                     "idempotent_replay": False, "error": ""})
    orch = Orchestrator(merge_arbiter=None, merge_gate=gate)
    asyncio.run(orch._arbitrate_task("t-1", BRANCHES))
    assert gate.grant_calls == []  # gate only consulted when arbiter exists


# ── MergeGate adapter ───────────────────────────────────────────


class FakeEngine:
    def __init__(self) -> None:
        self.issue_args: list[str] = []
        self.report_args: list[tuple] = []
        self.issue_result = {"granted": True,
                             "merge_idempotency_key": "k" * 32,
                             "idempotent_replay": False, "error": ""}
        self.report_result = {"accepted": True, "idempotent_replay": False}

    async def issue_merge_grant_async(self, graph_id: str) -> dict:
        self.issue_args.append(graph_id)
        return dict(self.issue_result)

    async def report_merge_outcome_async(
        self, graph_id: str, merge_idempotency_key: str, status: str,
        merged_branches, conflict_branches, push_status: str,
    ) -> dict:
        self.report_args.append(
            (graph_id, merge_idempotency_key, status, merged_branches,
             conflict_branches, push_status),
        )
        return dict(self.report_result)


def test_merge_gate_adapter_maps_to_engine_calls():
    engine = FakeEngine()
    gate = MergeGate(engine)
    decision = asyncio.run(gate.issue_merge_grant("t-1"))
    assert decision["granted"] is True
    assert engine.issue_args == ["t-1"]

    report = asyncio.run(
        gate.report_merge_outcome(
            "t-1", "k" * 32,
            {"status": "merged", "merged_branches": ["b1"],
             "conflict_branches": ["b2"], "push_status": "pushed"},
        ),
    )
    assert report == {"accepted": True, "idempotent_replay": False}
    assert engine.report_args == [
        ("t-1", "k" * 32, "merged", ["b1"], ["b2"], "pushed"),
    ]


def test_merge_gate_adapter_defaults_push_status():
    engine = FakeEngine()
    gate = MergeGate(engine)
    asyncio.run(
        gate.report_merge_outcome(
            "t-1", "k" * 32,
            {"status": "conflict", "merged_branches": [],
             "conflict_branches": ["b1"]},
        ),
    )
    assert engine.report_args[0][5] == "no_push"
