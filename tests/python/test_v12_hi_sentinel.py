"""Tests for the v12 HiSentinel pre-execution triage (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.hi_sentinel import (
    UC_HI_SENTINEL,
    ActionProposal,
    HiSentinel,
    SentinelDecision,
    hgid_helpfulness_gate,
    hi_sentinel_enabled,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UC_HI_SENTINEL, raising=False)
    assert hi_sentinel_enabled() is False
    monkeypatch.setenv(UC_HI_SENTINEL, "yes")
    assert hi_sentinel_enabled() is True


def test_supported_low_risk_action_is_allowed() -> None:
    verdict = HiSentinel().review(
        ActionProposal(action="read_file", target="src/a.py", has_evidence=True)
    )
    assert verdict.decision is SentinelDecision.ALLOW
    assert verdict.blocked is False


def test_protected_target_is_redirected() -> None:
    verdict = HiSentinel().review(
        ActionProposal(
            action="edit_file",
            target="tests/test_a.py",
            has_evidence=True,
            touches_protected=True,
        )
    )
    assert verdict.decision is SentinelDecision.REDIRECT
    assert "protected" in verdict.redirect_instruction


def test_edit_without_evidence_is_redirected() -> None:
    verdict = HiSentinel().review(
        ActionProposal(action="submit_patch", target="src/a.py", has_evidence=False)
    )
    assert verdict.decision is SentinelDecision.REDIRECT
    assert "evidence" in verdict.redirect_instruction


def test_missing_human_preference_is_hard_paused() -> None:
    verdict = HiSentinel().review(
        ActionProposal(
            action="edit_file",
            target="src/a.py",
            has_evidence=True,
            requires_human_preference=True,
            authorization_present=False,
        )
    )
    assert verdict.decision is SentinelDecision.HARD_PAUSE
    assert verdict.pause_question


def test_destructive_protected_action_is_hard_paused() -> None:
    verdict = HiSentinel().review(
        ActionProposal(
            action="delete_file",
            target="tests/test_a.py",
            has_evidence=True,
            touches_protected=True,
            destructive=True,
        )
    )
    assert verdict.decision is SentinelDecision.HARD_PAUSE


def test_hgid_gate_requires_same_label_and_more_confidence() -> None:
    gate = hgid_helpfulness_gate(
        {"allow": 0.82, "intervene": 0.18},
        {"allow": 0.61, "intervene": 0.39},
        "allow",
    )
    assert gate.helpful is True
    assert gate.teacher_label == "allow"

    wrong_label = hgid_helpfulness_gate(
        {"allow": 0.82, "intervene": 0.18},
        {"allow": 0.61, "intervene": 0.39},
        "intervene",
    )
    assert wrong_label.helpful is False

    flipped = hgid_helpfulness_gate(
        {"allow": 0.40, "intervene": 0.60},
        {"allow": 0.70, "intervene": 0.30},
        "allow",
    )
    assert flipped.helpful is False


def test_monitor_steps_require_the_full_causal_prefix() -> None:
    assert HiSentinel.prefix_mode(True) == "full_causal_prefix"
    assert HiSentinel.prefix_mode(False) == "incremental"
