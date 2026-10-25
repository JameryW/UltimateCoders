"""Structured completion_conditions (ticket 05 / D3): non-LLM evaluate seam.

Digest / ContractRegistry stay goal-change only. Completion evaluation is a
separate namespace exported from ``ultimate_coders.agent.contract``. Model
self-report keys alone cannot satisfy completion.
"""

from __future__ import annotations

import pytest
from ultimate_coders.agent.contract import (
    COMPLETION_CONDITIONS_ENFORCE_ENV,
    CompletionCondition,
    CompletionVerdict,
    ConditionResult,
    ContractRegistry,
    completion_conditions_enforce_enabled,
    completion_gate,
    contract_digest,
    evaluate_completion_conditions,
)
from ultimate_coders.runtime_state import RuntimeState

# ── feature flag ───────────────────────────────────────────────────


def test_completion_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(COMPLETION_CONDITIONS_ENFORCE_ENV, raising=False)
    assert completion_conditions_enforce_enabled() is False


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "off"])
def test_completion_flag_falsey_is_off(monkeypatch, value):
    monkeypatch.setenv(COMPLETION_CONDITIONS_ENFORCE_ENV, value)
    assert completion_conditions_enforce_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_completion_flag_truthy_is_on(monkeypatch, value):
    monkeypatch.setenv(COMPLETION_CONDITIONS_ENFORCE_ENV, value)
    assert completion_conditions_enforce_enabled() is True


# ── evaluate (pure; always computes) ───────────────────────────────


def test_empty_conditions_are_satisfied():
    verdict = evaluate_completion_conditions([], {})
    assert verdict.satisfied is True
    assert verdict.results == []


def test_evidence_flag_passes_when_evidence_is_true():
    conditions = [CompletionCondition(type="evidence_flag", params={"name": "tests_green"})]
    verdict = evaluate_completion_conditions(conditions, {"tests_green": True})
    assert verdict.satisfied is True
    assert len(verdict.results) == 1
    assert verdict.results[0] == ConditionResult(
        type="evidence_flag", passed=True, reason="evidence['tests_green'] is True"
    )


def test_evidence_flag_fails_when_missing_or_not_true():
    conditions = [{"type": "evidence_flag", "params": {"name": "tests_green"}}]
    for evidence in ({}, {"tests_green": False}, {"tests_green": "yes"}, {"tests_green": 1}):
        verdict = evaluate_completion_conditions(conditions, evidence)
        assert verdict.satisfied is False
        assert verdict.results[0].passed is False
        assert "tests_green" in verdict.results[0].reason


def test_all_conditions_must_pass():
    conditions = [
        CompletionCondition("evidence_flag", {"name": "a"}),
        CompletionCondition("evidence_flag", {"name": "b"}),
    ]
    verdict = evaluate_completion_conditions(conditions, {"a": True, "b": False})
    assert verdict.satisfied is False
    assert [r.passed for r in verdict.results] == [True, False]


def test_model_self_report_types_always_fail():
    for ctype in ("model_approved", "llm_done", "approved", "done"):
        conditions = [CompletionCondition(type=ctype, params={})]
        evidence = {"approved": True, "done": True, "llm_done": True, "model_approved": True}
        verdict = evaluate_completion_conditions(conditions, evidence)
        assert verdict.satisfied is False, ctype
        assert verdict.results[0].passed is False
        assert "self-report" in verdict.results[0].reason.lower() or "cannot" in verdict.results[
            0
        ].reason.lower()


def test_unknown_type_fails_closed():
    verdict = evaluate_completion_conditions(
        [CompletionCondition(type="verify_command", params={"cmd": "pytest"})],
        {"cmd_ok": True},
    )
    assert verdict.satisfied is False
    assert verdict.results[0].passed is False
    assert "unknown" in verdict.results[0].reason.lower()


def test_evaluate_accepts_dict_and_dataclass_mix():
    conditions = [
        {"type": "evidence_flag", "params": {"name": "ok"}},
        CompletionCondition("evidence_flag", {"name": "also"}),
    ]
    verdict = evaluate_completion_conditions(conditions, {"ok": True, "also": True})
    assert isinstance(verdict, CompletionVerdict)
    assert verdict.satisfied is True


def test_evaluate_computes_even_when_flag_off(monkeypatch):
    """Pure evaluate always computes; product callers use the flag / gate."""
    monkeypatch.delenv(COMPLETION_CONDITIONS_ENFORCE_ENV, raising=False)
    verdict = evaluate_completion_conditions(
        [CompletionCondition("evidence_flag", {"name": "x"})],
        {},
    )
    assert verdict.satisfied is False


# ── product gate helper ────────────────────────────────────────────


def test_completion_gate_satisfied_when_flag_off(monkeypatch):
    monkeypatch.delenv(COMPLETION_CONDITIONS_ENFORCE_ENV, raising=False)
    gate = completion_gate(
        [CompletionCondition("evidence_flag", {"name": "x"})],
        {},
    )
    assert gate.satisfied is True


def test_completion_gate_enforces_when_flag_on(monkeypatch):
    monkeypatch.setenv(COMPLETION_CONDITIONS_ENFORCE_ENV, "1")
    fail = completion_gate([CompletionCondition("evidence_flag", {"name": "x"})], {})
    assert fail.satisfied is False
    ok = completion_gate(
        [CompletionCondition("evidence_flag", {"name": "x"})],
        {"x": True},
    )
    assert ok.satisfied is True


# ── digest namespace unchanged ──────────────────────────────────────


def test_digest_apis_unchanged_and_separable(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)

    assert contract_digest("goal A") != contract_digest("goal B")
    registry = ContractRegistry(RuntimeState())
    first = registry.observe("t1", "goal A")
    assert (first.status, first.version) == ("first_seen", 1)

    # Completion evaluate must not touch registry records.
    evaluate_completion_conditions(
        [CompletionCondition("evidence_flag", {"name": "done"})],
        {"done": True},
    )
    assert registry.get("t1")["digest"] == contract_digest("goal A")
    assert registry.observe("t1", "goal A").status == "unchanged"
