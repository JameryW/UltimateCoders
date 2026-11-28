"""Tests for the v12 cross-harness HSE evolver (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.knowledge.hse_evolver import (
    UC_HSE_EVOLVER,
    HarnessKind,
    HseEvolver,
    PerformanceAntiPattern,
    SkillCard,
    SkillEvidence,
    audit_performance,
    hse_evolver_enabled,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UC_HSE_EVOLVER, raising=False)
    assert hse_evolver_enabled() is False
    monkeypatch.setenv(UC_HSE_EVOLVER, "on")
    assert hse_evolver_enabled() is True


def test_cross_harness_card_is_accepted() -> None:
    card = SkillCard(
        name="contract-first repair",
        description="Read the caller set before editing a shared helper.",
        applicability=("rust", "python"),
        procedure=("list callers", "read tests", "edit smallest surface"),
        evidence=(
            SkillEvidence(HarnessKind.PI, "glm-5.2", 10, 8),
            SkillEvidence(HarnessKind.CLAUDE_CODE, "opus-4.8", 10, 7),
        ),
    )
    evaluation = HseEvolver().evaluate(card)
    assert evaluation.generic is True
    assert evaluation.average_success_rate == pytest.approx(0.75)
    assert set(evaluation.positive_harnesses) == {HarnessKind.PI, HarnessKind.CLAUDE_CODE}


def test_single_harness_card_is_rejected() -> None:
    card = SkillCard(
        name="local trick",
        description="Only works on one harness.",
        evidence=(SkillEvidence(HarnessKind.PI, "glm-5.2", 10, 9),),
    )
    evaluation = HseEvolver().evaluate(card)
    assert evaluation.generic is False
    assert "need 2" in evaluation.reason


def test_evolve_keeps_the_strongest_generic_cards() -> None:
    strong = SkillCard(
        name="strong",
        description="high transfer",
        evidence=(
            SkillEvidence(HarnessKind.PI, "m", 10, 9),
            SkillEvidence(HarnessKind.CODEX, "m", 10, 8),
        ),
    )
    medium = SkillCard(
        name="medium",
        description="medium transfer",
        evidence=(
            SkillEvidence(HarnessKind.PI, "m", 10, 6),
            SkillEvidence(HarnessKind.CLAUDE_CODE, "m", 10, 6),
        ),
    )
    rejected = SkillCard(
        name="rejected",
        description="one harness only",
        evidence=(SkillEvidence(HarnessKind.PI, "m", 10, 9),),
    )
    report = HseEvolver(max_skills=1).evolve([medium, rejected, strong])
    assert tuple(card.name for card in report.accepted) == ("strong",)
    assert tuple(card.name for card in report.rejected) == ("rejected",)
    assert any(item.startswith("accepted: strong") for item in report.reasons)


def test_performance_audit_meets_target() -> None:
    audit = audit_performance(100.0, 50.0)
    assert audit.recovery_rate == pytest.approx(0.5)
    assert audit.passed is True
    assert audit.anti_patterns == ()
    assert "meets" in audit.diagnosis


def test_performance_audit_diagnoses_system_level_bottleneck() -> None:
    audit = audit_performance(
        100.0,
        80.0,
        [
            PerformanceAntiPattern.LOCK_CONTENTION,
            PerformanceAntiPattern.LOCK_CONTENTION,
            PerformanceAntiPattern.BLOCKING_IO,
        ],
    )
    assert audit.recovery_rate == pytest.approx(0.2)
    assert audit.passed is False
    assert audit.anti_patterns == (
        PerformanceAntiPattern.LOCK_CONTENTION,
        PerformanceAntiPattern.BLOCKING_IO,
    )
    assert "system-level bottleneck" in audit.diagnosis


def test_performance_audit_rejects_a_zero_baseline() -> None:
    with pytest.raises(ValueError, match="baseline_ms"):
        audit_performance(0.0, 10.0)
