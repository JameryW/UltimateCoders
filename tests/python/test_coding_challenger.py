"""Same-contract one-shot coding Champion/Challenger (V8 P2 / D8).

Covers pure helpers (no NATS) and a thin orchestrator wire smoke:
flag-off noop, challenge_once under unchanged digest, quota, no alternate,
eval_role=report stamp.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from ultimate_coders.agent.coding_challenger import (  # noqa: E402
    CHALLENGER_EPISODE_KEY,
    CHALLENGER_META_KEY,
    CODING_CHALLENGER_AGENT_ENV,
    CODING_CHALLENGER_ENFORCE_ENV,
    ChallengerDecision,
    ChallengerEpisodeState,
    apply_coding_challenger_decision,
    coding_challenger_enforce_enabled,
    decide_coding_challenger,
    finalize_coding_challenger_observation,
    is_report_challenger_episode,
    load_challenger_episode,
    maybe_apply_coding_challenger,
)
from ultimate_coders.agent.contract import contract_digest  # noqa: E402
from ultimate_coders.agent.orchestrator import Orchestrator  # noqa: E402
from ultimate_coders.agent.types import (  # noqa: E402
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
)


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CODING_CHALLENGER_ENFORCE_ENV, raising=False)
    assert coding_challenger_enforce_enabled() is False


def test_decide_noop_when_flag_off_or_not_success() -> None:
    episode = ChallengerEpisodeState()
    assert (
        decide_coding_challenger(
            enforce=False,
            success=True,
            episode=episode,
            current_agent="grok-build",
            available=["grok-build", "codex"],
        ).action
        == "noop"
    )
    assert (
        decide_coding_challenger(
            enforce=True,
            success=False,
            episode=episode,
            current_agent="grok-build",
            available=["grok-build", "codex"],
        ).action
        == "noop"
    )


def test_decide_challenge_once_on_first_success() -> None:
    decision = decide_coding_challenger(
        enforce=True,
        success=True,
        episode=ChallengerEpisodeState(contract_digest="deadbeef"),
        current_agent="grok-build",
        available=["grok-build", "codex", "opencode"],
        expected_digest="deadbeef",
    )
    assert decision.action == "challenge_once"
    assert decision.challenger_agent == "codex"


def test_decide_prefers_explicit_challenger() -> None:
    decision = decide_coding_challenger(
        enforce=True,
        success=True,
        episode=ChallengerEpisodeState(),
        current_agent="grok-build",
        available=["grok-build", "codex", "opencode"],
        preferred="opencode",
    )
    assert decision.action == "challenge_once"
    assert decision.challenger_agent == "opencode"


def test_decide_noop_when_quota_used_or_no_alternate() -> None:
    used = ChallengerEpisodeState(swaps_used=1, last_agent="codex")
    assert (
        decide_coding_challenger(
            enforce=True,
            success=True,
            episode=used,
            current_agent="codex",
            available=["grok-build", "codex"],
        ).action
        == "noop"
    )
    assert (
        decide_coding_challenger(
            enforce=True,
            success=True,
            episode=ChallengerEpisodeState(),
            current_agent="solo",
            available=["solo"],
        ).action
        == "noop"
    )


def test_decide_noop_on_digest_mismatch() -> None:
    decision = decide_coding_challenger(
        enforce=True,
        success=True,
        episode=ChallengerEpisodeState(contract_digest="aaa"),
        current_agent="grok-build",
        available=["grok-build", "codex"],
        expected_digest="bbb",
    )
    assert decision.action == "noop"
    assert "digest" in decision.reason.lower()


def test_apply_challenge_once_stamps_eval_role_and_preserves_digest() -> None:
    goal = "Ship the feature without touching main"
    digest = contract_digest(goal)
    subtask = Subtask(
        id="s1",
        parent_id="t1",
        description="impl",
        user_request=goal,
        status=SubtaskStatus.IN_PROGRESS,
        agent_config={"agent": "grok-build"},
        dispatch_retry_count=0,
        result=SubtaskResult(subtask_id="s1", success=True, summary="champion ok"),
    )
    episode = ChallengerEpisodeState(contract_digest=digest)
    decision = ChallengerDecision(
        action="challenge_once",
        challenger_agent="codex",
        reason="test challenge",
    )
    new_ep = apply_coding_challenger_decision(
        subtask,
        decision,
        episode,
        champion_summary="champion ok",
        champion_agent="grok-build",
    )
    assert subtask.status == SubtaskStatus.PENDING
    assert subtask.user_request == goal
    assert contract_digest(subtask.user_request) == digest
    assert subtask.agent_config["agent"] == "codex"
    assert subtask.agent_config["eval_role"] == "report"
    assert subtask.dispatch_retry_count == 1
    assert subtask.retry_count == 0
    assert subtask.result is None
    assert new_ep.swaps_used == 1
    assert new_ep.last_agent == "codex"
    assert load_challenger_episode(subtask.agent_config).swaps_used == 1
    assert CHALLENGER_EPISODE_KEY in subtask.agent_config
    meta = subtask.agent_config[CHALLENGER_META_KEY]
    assert meta["champion_summary"] == "champion ok"
    assert meta["champion_agent"] == "grok-build"
    assert meta["challenger_agent"] == "codex"


def test_maybe_apply_flag_off_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CODING_CHALLENGER_ENFORCE_ENV, raising=False)
    subtask = Subtask(
        id="s1",
        parent_id="t1",
        description="d",
        user_request="goal",
        status=SubtaskStatus.IN_PROGRESS,
        agent_config={"agent": "grok-build"},
    )
    before = dict(subtask.agent_config)
    decision = maybe_apply_coding_challenger(
        subtask,
        success=True,
        available=["grok-build", "codex"],
    )
    assert decision.action == "noop"
    assert subtask.status == SubtaskStatus.IN_PROGRESS
    assert subtask.agent_config == before


def test_maybe_apply_challenge_then_quota_noop() -> None:
    goal = "same contract forever"
    digest = contract_digest(goal)
    subtask = Subtask(
        id="s1",
        parent_id="t1",
        description="d",
        user_request=goal,
        status=SubtaskStatus.IN_PROGRESS,
        agent_config={"agent": "grok-build"},
    )
    first = maybe_apply_coding_challenger(
        subtask,
        success=True,
        available=["grok-build", "codex"],
        enforce=True,
        champion_summary="done",
    )
    assert first.action == "challenge_once"
    assert subtask.status == SubtaskStatus.PENDING
    assert subtask.agent_config["agent"] == "codex"
    assert subtask.agent_config["eval_role"] == "report"
    assert subtask.user_request == goal
    assert contract_digest(subtask.user_request) == digest

    # Challenger attempt succeeds — quota used → noop, keep Completed path.
    subtask.status = SubtaskStatus.IN_PROGRESS
    second = maybe_apply_coding_challenger(
        subtask,
        success=True,
        available=["grok-build", "codex"],
        enforce=True,
        current_agent="codex",
    )
    assert second.action == "noop"
    assert subtask.user_request == goal
    assert contract_digest(subtask.user_request) == digest
    assert load_challenger_episode(subtask.agent_config).swaps_used == 1


def test_maybe_apply_respects_env_preferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CODING_CHALLENGER_ENFORCE_ENV, "1")
    monkeypatch.setenv(CODING_CHALLENGER_AGENT_ENV, "opencode")
    subtask = Subtask(
        id="s1",
        parent_id="t1",
        description="d",
        user_request="goal",
        status=SubtaskStatus.IN_PROGRESS,
        agent_config={"agent": "grok-build"},
    )
    decision = maybe_apply_coding_challenger(
        subtask,
        success=True,
        available=["grok-build", "codex", "opencode"],
    )
    assert decision.action == "challenge_once"
    assert decision.challenger_agent == "opencode"
    assert subtask.agent_config["agent"] == "opencode"


@pytest.mark.asyncio
async def test_orchestrator_flag_off_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CODING_CHALLENGER_ENFORCE_ENV, raising=False)
    orch = Orchestrator()
    task = Task(id="t1", description="d", status=TaskStatus.IN_PROGRESS)
    st = Subtask(
        id="s1",
        parent_id="t1",
        description="d",
        user_request="keep digest",
        status=SubtaskStatus.IN_PROGRESS,
        agent_config={"agent": "grok-build"},
        assigned_worker="w1",
    )
    task.subtasks = [st]
    orch.tasks[task.id] = task
    orch.workers["w1"] = type("W", (), {"current_load": 1})()  # type: ignore[assignment]

    result = SubtaskResult(
        subtask_id="s1",
        worker_id="w1",
        success=True,
        summary="ok",
    )
    await orch.handle_subtask_result(result)
    assert st.status == SubtaskStatus.COMPLETED
    assert st.agent_config.get("agent") == "grok-build"
    assert load_challenger_episode(st.agent_config).swaps_used == 0
    assert st.result is result


@pytest.mark.asyncio
async def test_orchestrator_challenge_once_then_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CODING_CHALLENGER_ENFORCE_ENV, "1")
    orch = Orchestrator()
    goal = "unchanged goal text"
    digest = contract_digest(goal)
    task = Task(id="t1", description=goal, status=TaskStatus.IN_PROGRESS)
    st = Subtask(
        id="s1",
        parent_id="t1",
        description="d",
        user_request=goal,
        status=SubtaskStatus.IN_PROGRESS,
        agent_config={"agent": "grok-build"},
        assigned_worker="w1",
        dispatch_retry_count=0,
    )
    task.subtasks = [st]
    orch.tasks[task.id] = task

    class _Entry:
        current_load = 1

    orch.workers["w1"] = _Entry()  # type: ignore[assignment]

    def _agents():
        return ["grok-build", "codex", "opencode"]

    monkeypatch.setattr(
        "ultimate_coders.agent.registry.available_agents",
        _agents,
    )

    first = SubtaskResult(
        subtask_id="s1",
        worker_id="w1",
        success=True,
        summary="champion done",
    )
    await orch.handle_subtask_result(first)
    assert st.status == SubtaskStatus.PENDING
    assert st.agent_config["agent"] == "codex"
    assert st.agent_config["eval_role"] == "report"
    assert st.user_request == goal
    assert contract_digest(st.user_request) == digest
    assert st.dispatch_retry_count == 1
    assert load_challenger_episode(st.agent_config).swaps_used == 1
    assert st.agent_config[CHALLENGER_META_KEY]["champion_summary"] == "champion done"
    assert task.status == TaskStatus.IN_PROGRESS
    assert orch.workers["w1"].current_load == 0

    # Duplicate champion success while Pending must be ignored.
    orch.workers["w1"].current_load = 0
    await orch.handle_subtask_result(first)
    assert st.status == SubtaskStatus.PENDING
    assert orch.workers["w1"].current_load == 0

    # Challenger success → Completed (quota already used).
    st.status = SubtaskStatus.IN_PROGRESS
    st.assigned_worker = "w1"
    orch.workers["w1"].current_load = 1
    second = SubtaskResult(
        subtask_id="s1",
        worker_id="w1",
        success=True,
        summary="challenger report",
    )
    await orch.handle_subtask_result(second)
    assert st.status == SubtaskStatus.COMPLETED
    assert st.result is second
    assert st.user_request == goal
    assert contract_digest(st.user_request) == digest
    assert st.agent_config["agent"] == "codex"
    assert st.agent_config["eval_role"] == "report"
    assert load_challenger_episode(st.agent_config).swaps_used == 1
    assert st.agent_config[CHALLENGER_META_KEY]["challenger_success"] is True
    assert st.agent_config[CHALLENGER_META_KEY]["challenger_summary"] == "challenger report"
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_orchestrator_challenger_failure_keeps_champion_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observe-only: report-role challenger failure must not demote champion."""
    monkeypatch.setenv(CODING_CHALLENGER_ENFORCE_ENV, "1")
    monkeypatch.delenv("UC_STALL_REDISPATCH_ENFORCE", raising=False)
    orch = Orchestrator()
    goal = "champion must stand"
    task = Task(id="t1", description=goal, status=TaskStatus.IN_PROGRESS)
    st = Subtask(
        id="s1",
        parent_id="t1",
        description="d",
        user_request=goal,
        status=SubtaskStatus.IN_PROGRESS,
        agent_config={"agent": "grok-build"},
        assigned_worker="w1",
        dispatch_retry_count=0,
    )
    task.subtasks = [st]
    orch.tasks[task.id] = task

    class _Entry:
        current_load = 1

    orch.workers["w1"] = _Entry()  # type: ignore[assignment]
    monkeypatch.setattr(
        "ultimate_coders.agent.registry.available_agents",
        lambda: ["grok-build", "codex"],
    )

    await orch.handle_subtask_result(
        SubtaskResult(
            subtask_id="s1",
            worker_id="w1",
            success=True,
            summary="champion ok",
        )
    )
    assert st.status == SubtaskStatus.PENDING
    assert st.agent_config["eval_role"] == "report"
    assert is_report_challenger_episode(st.agent_config)

    st.status = SubtaskStatus.IN_PROGRESS
    st.assigned_worker = "w1"
    orch.workers["w1"].current_load = 1
    await orch.handle_subtask_result(
        SubtaskResult(
            subtask_id="s1",
            worker_id="w1",
            success=False,
            summary="challenger boom",
            error="report agent crashed",
            stall_signal={
                "loop_detected": True,
                "stalled_streak": 3,
                "reason": "stop",
                "error_signature": "x",
            },
        )
    )
    assert st.status == SubtaskStatus.COMPLETED
    assert st.result is not None
    assert st.result.success is True
    assert st.result.summary == "champion ok"
    meta = st.agent_config[CHALLENGER_META_KEY]
    assert meta["challenger_success"] is False
    assert meta["challenger_summary"] == "challenger boom"
    assert meta["challenger_error"] == "report agent crashed"
    assert meta["champion_summary"] == "champion ok"
    assert task.status == TaskStatus.COMPLETED


def test_finalize_observation_preserves_challenger_success_result() -> None:
    subtask = Subtask(
        id="s1",
        parent_id="t1",
        description="d",
        agent_config={
            "agent": "codex",
            "eval_role": "report",
            CHALLENGER_EPISODE_KEY: {"swaps_used": 1, "last_agent": "codex"},
            CHALLENGER_META_KEY: {
                "champion_agent": "grok-build",
                "champion_summary": "champ",
            },
        },
    )
    result = SubtaskResult(subtask_id="s1", success=True, summary="challenger ok")
    out = finalize_coding_challenger_observation(subtask, result)
    assert out is result
    assert subtask.agent_config[CHALLENGER_META_KEY]["challenger_success"] is True


@pytest.mark.asyncio
async def test_orchestrator_no_alternate_keeps_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CODING_CHALLENGER_ENFORCE_ENV, "1")
    orch = Orchestrator()
    task = Task(id="t1", description="d", status=TaskStatus.IN_PROGRESS)
    st = Subtask(
        id="s1",
        parent_id="t1",
        description="d",
        user_request="solo goal",
        status=SubtaskStatus.IN_PROGRESS,
        agent_config={"agent": "solo"},
        assigned_worker="w1",
    )
    task.subtasks = [st]
    orch.tasks[task.id] = task
    orch.workers["w1"] = type("W", (), {"current_load": 1})()  # type: ignore[assignment]
    monkeypatch.setattr(
        "ultimate_coders.agent.registry.available_agents",
        lambda: ["solo"],
    )

    result = SubtaskResult(
        subtask_id="s1",
        worker_id="w1",
        success=True,
        summary="ok",
    )
    await orch.handle_subtask_result(result)
    assert st.status == SubtaskStatus.COMPLETED
    assert st.agent_config.get("agent") == "solo"
    assert load_challenger_episode(st.agent_config).swaps_used == 0


@pytest.mark.asyncio
async def test_orchestrator_failure_does_not_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CODING_CHALLENGER_ENFORCE_ENV, "1")
    monkeypatch.delenv("UC_STALL_REDISPATCH_ENFORCE", raising=False)
    orch = Orchestrator()
    task = Task(id="t1", description="d", status=TaskStatus.IN_PROGRESS)
    st = Subtask(
        id="s1",
        parent_id="t1",
        description="d",
        user_request="fail path",
        status=SubtaskStatus.IN_PROGRESS,
        agent_config={"agent": "grok-build"},
        assigned_worker="w1",
    )
    task.subtasks = [st]
    orch.tasks[task.id] = task
    orch.workers["w1"] = type("W", (), {"current_load": 1})()  # type: ignore[assignment]
    monkeypatch.setattr(
        "ultimate_coders.agent.registry.available_agents",
        lambda: ["grok-build", "codex"],
    )

    result = SubtaskResult(
        subtask_id="s1",
        worker_id="w1",
        success=False,
        summary="boom",
    )
    await orch.handle_subtask_result(result)
    assert st.status == SubtaskStatus.FAILED
    assert st.agent_config.get("agent") == "grok-build"
    assert load_challenger_episode(st.agent_config).swaps_used == 0
