"""Tests for the knowledge-layer read face (counts only, never content)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request
from ultimate_coders.dashboard.app import DashboardApp
from ultimate_coders.knowledge.attempt_chain import AttemptChainLedger, AttemptRound
from ultimate_coders.knowledge.claims import (
    attempt_scope,
    record_delivery_claim,
    record_failure_claim,
    validate_and_promote_delivery,
)
from ultimate_coders.knowledge.dashboard import (
    SCHEMA,
    aggregate_knowledge,
    main,
    render_text,
)
from ultimate_coders.knowledge.evidence import EvidenceLedger
from ultimate_coders.knowledge.gate import REASON_ATTEMPT_NOT_SUCCESSFUL, REASON_GRAPH_SCOPE_MISSING
from ultimate_coders.knowledge.model import (
    AttemptRef,
    EvidenceKind,
    KnowledgeScope,
    Visibility,
)
from ultimate_coders.knowledge.pages import KnowledgePages
from ultimate_coders.knowledge.rollout import KnowledgeStage
from ultimate_coders.runtime_state import RuntimeState

REVISION = "a" * 40


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    monkeypatch.delenv("UC_KNOWLEDGE_STAGE", raising=False)
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", "")


@pytest.fixture()
def state(tmp_path):
    return RuntimeState(tmp_path / "dashboard-state.sqlite3", database_url="")


def _ledger(state, stage: KnowledgeStage = KnowledgeStage.GRAPH) -> EvidenceLedger:
    return EvidenceLedger(state, stage=stage)


def _ref(task="t1", subtask="s1", attempt=1) -> AttemptRef:
    return AttemptRef(
        task_id=task, subtask_id=subtask, dispatch_attempt=0, attempt=attempt, graph_id=task
    )


def _outcome(ledger: EvidenceLedger, ref: AttemptRef, *, success: bool, **extra):
    payload = {"success": success, "retryable": not success, "attempt": ref.attempt}
    payload.update(extra)
    return ledger.record_evidence(
        attempt=ref,
        kind=EvidenceKind.ATTEMPT_OUTCOME,
        payload=payload,
        scope=attempt_scope(ref),
    )


def _allowed_delivery(ledger: EvidenceLedger, ref: AttemptRef) -> None:
    receipt = ledger.record_evidence(
        attempt=ref,
        kind=EvidenceKind.MERGE_RECEIPT,
        payload={"status": "merged", "commit_sha": REVISION},
        scope=attempt_scope(ref),
    )
    claim = record_delivery_claim(
        ledger,
        attempt=ref,
        scope=attempt_scope(ref),
        revision=REVISION,
        evidence_ids=(receipt.evidence_id,),
        merge_status="merged",
        file_count=1,
    )
    validate_and_promote_delivery(
        ledger,
        claim.claim_id,
        revision=REVISION,
        evidence_ids=(receipt.evidence_id,),
        push_status="pushed",
    )


def _denied_delivery(ledger: EvidenceLedger, ref: AttemptRef) -> None:
    receipt = ledger.record_evidence(
        attempt=ref,
        kind=EvidenceKind.MERGE_RECEIPT,
        payload={"status": "merged", "commit_sha": REVISION},
        scope=attempt_scope(ref),
    )
    claim = record_delivery_claim(
        ledger,
        attempt=ref,
        scope=attempt_scope(ref),
        revision=REVISION,
        evidence_ids=(receipt.evidence_id,),
        merge_status="merged",
        file_count=2,
    )
    validate_and_promote_delivery(
        ledger,
        claim.claim_id,
        revision=REVISION,
        evidence_ids=(receipt.evidence_id,),
        push_status="pushed",
        attempt_succeeded=False,
    )


class TestAggregate:
    def test_empty_state_reports_zeroes(self, state):
        overview = aggregate_knowledge(state)

        assert overview["schema"] == SCHEMA
        assert overview["truncated"] is False
        assert overview["scanned"] == {"evidence": 0, "claims": 0, "attempts": 0, "pages": 0}
        assert overview["attempts"]["total"] == 0
        assert overview["evidence"] == {
            "total": 0,
            "by_kind": {},
            "by_visibility": {},
            "verified": 0,
        }
        assert overview["claims"]["total"] == 0
        assert overview["gate"] == {"allowed": 0, "denied": 0, "other": 0, "reasons": {}}
        assert overview["pages"]["total"] == 0

    def test_counts_attempts_evidence_and_claims(self, state):
        ledger = _ledger(state)
        good, bad = _ref(attempt=1), _ref(attempt=2)
        _outcome(ledger, good, success=True)
        _outcome(ledger, bad, success=False)
        ledger.record_evidence(
            attempt=bad,
            kind=EvidenceKind.FAILURE_SIGNATURE,
            payload={"signature": "ValueError: <n> bad"},
            scope=attempt_scope(bad),
        )
        record_failure_claim(
            ledger,
            attempt=bad,
            scope=attempt_scope(bad),
            evidence_ids=(),
            signature="ValueError: <n> bad",
        )
        rounds = AttemptChainLedger(state)
        rounds.record(AttemptRound(attempt=good, outcome="completed"))
        rounds.record(AttemptRound(attempt=bad, outcome="failed"))

        overview = aggregate_knowledge(state)

        assert overview["scanned"] == {"evidence": 3, "claims": 1, "attempts": 2, "pages": 0}
        assert overview["attempts"]["by_outcome"] == {"completed": 1, "failed": 1}
        assert overview["attempts"]["completed"] == 1
        assert overview["attempts"]["failed"] == 1
        assert overview["evidence"]["by_kind"] == {
            "attempt_outcome": 2,
            "failure_signature": 1,
        }
        assert overview["evidence"]["verified"] == 0
        assert overview["claims"]["by_status"] == {"candidate": 1}

    def test_gate_decisions_become_a_reason_histogram(self, state):
        ledger = _ledger(state)
        _allowed_delivery(ledger, _ref(attempt=1))
        _denied_delivery(ledger, _ref(attempt=2))

        gate = aggregate_knowledge(state)["gate"]

        assert gate["allowed"] == 1
        assert gate["denied"] == 1
        assert gate["reasons"] == {REASON_ATTEMPT_NOT_SUCCESSFUL: 1}

    def test_graph_scope_denial_reason_is_recorded(self, state):
        ledger = _ledger(state)
        ref = _ref(attempt=1)
        receipt = ledger.record_evidence(
            attempt=ref,
            kind=EvidenceKind.MERGE_RECEIPT,
            payload={"status": "merged"},
        )
        claim = record_delivery_claim(
            ledger,
            attempt=ref,
            scope=KnowledgeScope(),  # no graph id: the gate must refuse graph sharing
            revision=REVISION,
            evidence_ids=(receipt.evidence_id,),
            merge_status="merged",
            file_count=1,
        )
        validate_and_promote_delivery(
            ledger, claim.claim_id, revision=REVISION, push_status="pushed"
        )

        gate = aggregate_knowledge(state)["gate"]

        assert gate["denied"] == 1
        assert gate["reasons"] == {REASON_GRAPH_SCOPE_MISSING: 1}

    def test_replayed_denial_does_not_inflate_the_histogram(self, state):
        ledger = _ledger(state)
        ref = _ref(attempt=1)
        record = _outcome(ledger, ref, success=False)
        claim = record_failure_claim(
            ledger,
            attempt=ref,
            scope=attempt_scope(ref),
            evidence_ids=(record.evidence_id,),
            signature="boom",
        )
        from ultimate_coders.knowledge.claims import promote_failure_claim

        promote_failure_claim(ledger, claim.claim_id)
        promote_failure_claim(ledger, claim.claim_id)

        gate = aggregate_knowledge(state)["gate"]
        validation_rows = [
            row
            for row in state.records("knowledge_evidence")
            if row.get("kind") == EvidenceKind.VALIDATION.value
        ]

        assert gate["denied"] == 1
        assert gate["reasons"] == {
            REASON_ATTEMPT_NOT_SUCCESSFUL: 1,
            "validation_missing": 1,
        }
        assert len(validation_rows) == 1

    def test_pages_are_visible_as_a_lifecycle_projection(self, state):
        ledger = _ledger(state)
        evidence = _outcome(ledger, _ref(), success=True)
        pages = KnowledgePages(state)
        pages.add(
            scope=KnowledgeScope(graph_id="t1"),
            title="auth notes",
            body="secret body text",
            evidence_ids=[evidence.evidence_id],
        )

        overview = aggregate_knowledge(state, task_id="t1")

        assert overview["pages"]["total"] == 1
        assert overview["pages"]["by_lifecycle"] == {"active": 1}
        assert overview["pages"]["locked"] == 0

    def test_task_filter_isolates_records_and_pages(self, state):
        ledger = _ledger(state)
        _outcome(ledger, _ref(task="t1"), success=True)
        _outcome(ledger, _ref(task="t2", subtask="s2"), success=False)
        KnowledgePages(state).add(
            scope=KnowledgeScope(graph_id="t2"),
            title="other",
            evidence_ids=["ev-1"],
        )

        t1 = aggregate_knowledge(state, task_id="t1")
        t2 = aggregate_knowledge(state, task_id="t2")

        assert t1["scanned"]["evidence"] == 1
        assert t1["pages"]["total"] == 0
        assert t2["scanned"]["evidence"] == 1
        assert t2["evidence"]["by_kind"] == {"attempt_outcome": 1}
        assert t2["pages"]["total"] == 1

    def test_limit_truncates_each_namespace(self, state):
        ledger = _ledger(state)
        for index in range(3):
            _outcome(ledger, _ref(subtask=f"s{index}"), success=True)

        overview = aggregate_knowledge(state, limit=2)

        assert overview["truncated"] is True
        assert overview["evidence"]["total"] == 2
        assert overview["scanned"]["evidence"] == 2


class TestRendering:
    def test_overview_never_carries_captured_content(self, state):
        ledger = _ledger(state)
        ref = _ref()
        record = _outcome(ledger, ref, success=False)
        record_failure_claim(
            ledger,
            attempt=ref,
            scope=attempt_scope(ref),
            evidence_ids=(record.evidence_id,),
            signature="SECRET_SIGNATURE",
        )
        KnowledgePages(state).add(
            scope=KnowledgeScope(graph_id="t1"),
            title="SECRET_TITLE",
            body="SECRET_BODY",
            evidence_ids=[record.evidence_id],
        )

        dumped = json.dumps(aggregate_knowledge(state), sort_keys=True)

        assert "SECRET" not in dumped
        assert "secret" not in dumped.lower()

    def test_render_text_is_a_short_summary(self, state):
        _allowed_delivery(_ledger(state), _ref())

        text = render_text(aggregate_knowledge(state))

        assert "knowledge overview" in text
        assert "stage" in text
        assert "gate: allowed 1" in text
        assert "SECRET" not in text

    def test_cli_emits_json(self, state, tmp_path, capsys):
        _outcome(_ledger(state), _ref(), success=True)

        code = main(["--state", str(tmp_path / "dashboard-state.sqlite3"), "--json"])

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema"] == SCHEMA
        assert payload["evidence"]["total"] == 1

    def test_cli_rejects_a_missing_state_argument(self):
        with pytest.raises(SystemExit):
            main([])


def test_visibility_defaults_stay_attempt_scoped(state):
    """A summary must never claim repository visibility the gate did not grant."""

    ledger = _ledger(state)
    _denied_delivery(ledger, _ref())

    claims = aggregate_knowledge(state)["claims"]

    assert claims["by_visibility"] == {Visibility.ATTEMPT.value: 1}
    assert claims["promoted"] == 0


# ── HTTP read surface (/dashboard/api/knowledge) ─────────────────────────


def _request(query: str = "", host: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/dashboard/api/knowledge",
            "query_string": query.encode(),
            "headers": [],
            "client": (host, 12345),
        }
    )


def _endpoint(app: DashboardApp):
    for route in app._app.routes:
        if getattr(route, "path", None) == "/dashboard/api/knowledge":
            return route.endpoint
    raise AssertionError("knowledge route not registered")


@pytest.fixture()
def dashboard_app(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    return DashboardApp(orchestrator=MagicMock())


async def test_knowledge_route_returns_counts_and_no_content(dashboard_app, tmp_path):
    state = RuntimeState()
    ledger = _ledger(state)
    _allowed_delivery(ledger, _ref())
    anchor = _outcome(ledger, _ref(), success=True)
    KnowledgePages(state).add(
        scope=KnowledgeScope(graph_id="t1"),
        title="SECRET_TITLE",
        body="SECRET_BODY",
        evidence_ids=[anchor.evidence_id],
    )

    resp = await _endpoint(dashboard_app)(_request("task_id=t1"))

    payload = json.loads(resp.body)
    assert resp.status_code == 200
    assert payload["schema"] == SCHEMA
    assert payload["claims"]["promoted"] == 1
    assert payload["pages"]["total"] == 1
    assert payload["gate"]["allowed"] == 1
    assert "SECRET" not in resp.body.decode()


async def test_knowledge_route_defaults_to_every_task(dashboard_app):
    state = RuntimeState()
    ledger = _ledger(state)
    _allowed_delivery(ledger, _ref(task="t1", subtask="s1"))
    _allowed_delivery(ledger, _ref(task="t2", subtask="s2"))

    resp = await _endpoint(dashboard_app)(_request())

    payload = json.loads(resp.body)
    assert resp.status_code == 200
    assert payload["claims"]["total"] == 2
    assert payload["task_id"] == ""


async def test_knowledge_route_rejects_invalid_limit(dashboard_app):
    endpoint = _endpoint(dashboard_app)

    for query in ("limit=0", "limit=9999", "limit=abc"):
        resp = await endpoint(_request(query))
        assert resp.status_code == 400, query
        assert "Invalid limit" in resp.body.decode()


async def test_knowledge_route_requires_auth_for_remote_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    app = DashboardApp(orchestrator=MagicMock())
    endpoint = _endpoint(app)

    denied = await endpoint(_request(host="10.0.0.5"))
    assert denied.status_code == 401

    allowed = await endpoint(_request("token=s3cret", host="10.0.0.5"))
    assert allowed.status_code == 200

    local = await endpoint(_request(host="127.0.0.1"))
    # Localhost is no longer an implicit bypass: the dashboard can be bound to
    # 0.0.0.0 behind a same-host reverse proxy, where every remote caller looks
    # local. Opting back in requires UC_DASHBOARD_TRUST_LOCALHOST=1.
    assert local.status_code == 401
