from __future__ import annotations

import pytest
from ultimate_coders.agent.adv_spec_reasoner import (
    ADV_SPEC_REASONER_ENV,
    AdversarialSpeculativeReasoner,
    DraftModel,
    ReasoningFork,
    adv_spec_reasoner_enabled,
)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ADV_SPEC_REASONER_ENV, raising=False)
    assert not adv_spec_reasoner_enabled()
    monkeypatch.setenv(ADV_SPEC_REASONER_ENV, "on")
    assert adv_spec_reasoner_enabled()


def test_trainable_parameter_count_is_tiny() -> None:
    model = DraftModel("draft-1b", 1_000_000_000, trainable_fraction=0.001)
    assert model.trainable_parameters == 1_000_000


def test_trainable_fraction_outside_published_range_is_rejected() -> None:
    with pytest.raises(ValueError):
        DraftModel("draft", 100, trainable_fraction=0.0001)
    with pytest.raises(ValueError):
        DraftModel("draft", 100, trainable_fraction=0.1)


def test_critical_low_confidence_fork_restarts() -> None:
    decision = AdversarialSpeculativeReasoner().should_restart(
        ReasoningFork(step=7, entropy=1.5, target_confidence=0.2, critical=True)
    )
    assert decision.restart
    assert "force target resample" in decision.reason


def test_non_critical_fork_does_not_restart() -> None:
    decision = AdversarialSpeculativeReasoner().should_restart(
        ReasoningFork(step=7, entropy=1.5, target_confidence=0.2, critical=False)
    )
    assert not decision.restart


def test_high_confidence_fork_does_not_restart() -> None:
    decision = AdversarialSpeculativeReasoner().should_restart(
        ReasoningFork(step=7, entropy=3.0, target_confidence=0.9)
    )
    assert not decision.restart


def test_plan_collects_restart_steps_and_model_density() -> None:
    model = DraftModel("draft-7b", 7_000_000_000, trainable_fraction=0.002)
    plan = AdversarialSpeculativeReasoner().plan(
        model,
        (
            ReasoningFork(1, 2.0, 0.1),
            ReasoningFork(2, 0.1, 0.1),
            ReasoningFork(3, 2.0, 0.1, critical=False),
        ),
    )
    assert plan.restart_steps == (1,)
    assert plan.draft_trainable_parameters == 14_000_000


def test_density_returns_trainable_fraction() -> None:
    model = DraftModel("draft", 1000, trainable_fraction=0.001)
    assert AdversarialSpeculativeReasoner().density(model) == 0.001


def test_invalid_fork_is_rejected() -> None:
    with pytest.raises(ValueError):
        ReasoningFork(step=-1, entropy=1.0, target_confidence=0.1)
    with pytest.raises(ValueError):
        ReasoningFork(step=1, entropy=-1.0, target_confidence=0.1)
    with pytest.raises(ValueError):
        ReasoningFork(step=1, entropy=1.0, target_confidence=2.0)


def test_invalid_thresholds_are_rejected() -> None:
    with pytest.raises(ValueError):
        AdversarialSpeculativeReasoner(entropy_threshold=0)
    with pytest.raises(ValueError):
        AdversarialSpeculativeReasoner(target_confidence=-0.1)
