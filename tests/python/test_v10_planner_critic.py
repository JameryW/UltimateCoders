"""Tests for the v10 Plan Critic exploration review (#23)."""

from __future__ import annotations

from ultimate_coders.agent.planner_critic import (
    PlanCritic,
    PlanCritique,
    critic_prompt,
    planner_critic_enabled,
)


def test_low_recall_produces_corrections_and_rejects() -> None:
    critic = PlanCritic()
    critique = critic.review(
        {"required": ["auth", "billing", "search", "reporting"]},
        [{"target": "auth", "evidence": ["auth.py:12"]}],
    )
    assert isinstance(critique, PlanCritique)
    assert critique.exploration_recall == 0.25
    assert critique.approved is False
    assert any("billing" in item for item in critique.corrections)
    assert any("search" in item for item in critique.corrections)


def test_high_coverage_approves() -> None:
    critic = PlanCritic()
    observations = [
        {"target": "auth", "evidence": ["auth.py:12"]},
        {"target": "billing", "evidence": ["billing.py:40"]},
        {"target": "search", "evidence": ["search.py:7"]},
        {"target": "reporting", "evidence": ["report.py:3"]},
    ]
    critique = critic.review(["auth", "billing", "search", "reporting"], observations)
    assert critique.exploration_recall == 1.0
    assert critique.exploration_precision == 1.0
    assert critique.approved is True
    assert critique.corrections == ()


def test_bare_observation_is_a_correction() -> None:
    critique = PlanCritic().review(["auth"], [{"target": "auth", "evidence": []}])
    assert critique.exploration_recall == 1.0
    assert critique.exploration_precision == 0.0
    assert critique.approved is False
    assert any("evidence" in item for item in critique.corrections)


def test_empty_observations_have_zero_precision() -> None:
    critique = PlanCritic().review(["auth"], [])
    assert critique.exploration_precision == 0.0
    assert critique.exploration_recall == 0.0
    assert critique.approved is False


def test_critic_prompt_is_bounded() -> None:
    critic = PlanCritic(max_corrections=10)
    required = [f"area-{index}" for index in range(20)]
    critique = critic.review({"required": required}, [])
    prompt = critic_prompt(critique)
    assert len(prompt.split()) <= 150
    assert "NOT APPROVED" in prompt
    assert "recall" in prompt


def test_custom_max_words() -> None:
    critique = PlanCritic().review(["a"], [])
    assert len(critic_prompt(critique, max_words=10).split()) <= 10


def test_accepts_object_spec_with_target_files() -> None:
    from types import SimpleNamespace

    spec = SimpleNamespace(
        target_files=[SimpleNamespace(path="pkg/a.py"), SimpleNamespace(path="pkg/b.py")]
    )
    critique = PlanCritic().review(
        spec,
        [
            {"target": "pkg/a.py", "evidence": ["read"]},
            {"target": "pkg/b.py", "evidence": ["read"]},
        ],
    )
    assert critique.exploration_recall == 1.0
    assert critique.approved is True


def test_flag(monkeypatch) -> None:
    monkeypatch.delenv("UC_PLANNER_CRITIC", raising=False)
    assert planner_critic_enabled() is False
    monkeypatch.setenv("UC_PLANNER_CRITIC", "1")
    assert planner_critic_enabled() is True
    monkeypatch.setenv("UC_PLANNER_CRITIC", "disabled")
    assert planner_critic_enabled() is False
