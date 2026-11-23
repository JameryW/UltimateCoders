"""Tests for V22 ledger item #93 (Reglos memory)."""

from __future__ import annotations

import pytest
from ultimate_coders.search.reglos_memory import (
    AppendOnlyMemory,
    CrossFamilyCritic,
    FrozenEvaluator,
    HarnessProposal,
    MemoryKind,
    ReglosLoop,
    ReglosMemoryError,
)


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_REGLOS_MEMORY", raising=False)
    from ultimate_coders.search.reglos_memory import reglos_memory_enabled

    assert reglos_memory_enabled() is False


def test_latest_preference_wins_and_history_is_kept() -> None:
    memory = AppendOnlyMemory()
    memory.append("theme", "dark")
    memory.append("theme", "light")
    assert memory.active("theme").value == "light"
    assert len(memory.history("theme")) == 2


def test_forget_appends_tombstone_and_hides_value() -> None:
    memory = AppendOnlyMemory()
    memory.append("theme", "dark")
    memory.forget("theme")
    assert memory.active("theme") is None
    assert memory.snapshot() == ()
    assert memory.history("theme")[-1].kind is MemoryKind.TOMBSTONE


def test_snapshot_sorted_by_key() -> None:
    memory = AppendOnlyMemory()
    memory.append("z", "1")
    memory.append("a", "2")
    assert [record.key for record in memory.snapshot()] == ["a", "z"]


def test_same_family_critic_is_rejected() -> None:
    review = CrossFamilyCritic().review(
        HarnessProposal("p1", "openai", "patch"), critic_family="openai"
    )
    assert review.approved is False
    assert "different model family" in review.reasons[0]


def test_gaming_flags_are_rejected() -> None:
    review = CrossFamilyCritic().review(
        HarnessProposal("p1", "openai", "patch", gaming_flags=("test-deletion",)),
        critic_family="anthropic",
    )
    assert review.approved is False
    assert "benchmark gaming" in review.reasons[0]


def test_frozen_evaluator_compares_to_baseline() -> None:
    evaluator = FrozenEvaluator(0.5)
    proposal = HarnessProposal("p1", "openai", "patch")
    assert evaluator.evaluate(proposal, 0.5).accepted is True
    assert evaluator.evaluate(proposal, 0.49).accepted is False


def test_accepted_proposal_appends_fact() -> None:
    memory = AppendOnlyMemory()
    loop = ReglosLoop(memory, CrossFamilyCritic(), FrozenEvaluator(0.5))
    decision = loop.evaluate_proposal(
        HarnessProposal("p1", "openai", "tighten retry", claimed_score=0.8),
        critic_family="anthropic",
        measured_score=0.7,
    )
    assert decision.accepted is True
    assert memory.active("harness:p1").kind is MemoryKind.FACT


def test_rejected_proposal_does_not_write_memory() -> None:
    memory = AppendOnlyMemory()
    loop = ReglosLoop(memory, CrossFamilyCritic(), FrozenEvaluator(0.5))
    decision = loop.evaluate_proposal(
        HarnessProposal("p1", "openai", "patch", gaming_flags=("hardcode",)),
        critic_family="anthropic",
        measured_score=0.9,
    )
    assert decision.accepted is False
    assert len(memory) == 0


def test_invalid_score_is_rejected() -> None:
    with pytest.raises(ReglosMemoryError):
        HarnessProposal("p1", "openai", "patch", claimed_score=1.1)
