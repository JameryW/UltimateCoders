"""Same-contract one-shot agent redispatch (T14 / D7 enforce half).

Covers pure helpers (no NATS) and a thin orchestrator wire smoke:
flag-off noop, swap-once under unchanged digest, second-stall escalate.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from ultimate_coders.agent import progress as progress_mod  # noqa: E402
from ultimate_coders.agent.contract import contract_digest  # noqa: E402
from ultimate_coders.agent.orchestrator import Orchestrator  # noqa: E402
from ultimate_coders.agent.progress import ProgressVerdict  # noqa: E402
from ultimate_coders.agent.stall_redispatch import (  # noqa: E402
    STALL_EPISODE_KEY,
    STALL_REDISPATCH_ENFORCE_ENV,
    RedispatchDecision,
    StallEpisodeState,
    StallSignal,
    apply_stall_redispatch_decision,
    build_stall_signal,
    current_agent_from_config,
    decide_stall_redispatch,
    load_stall_episode,
    maybe_apply_stall_redispatch,
    select_alternate_agent,
    stall_redispatch_enforce_enabled,
    stall_signal_from_mapping,
)
from ultimate_coders.agent.types import (  # noqa: E402
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
)

_FORBIDDEN_PROGRESS_NAMES = frozenset(
    {
        "replan",
        "redispatch",
        "swap_agent",
        "agent_swap",
        "alternate_agent",
        "stall_redispatch_enforce_enabled",
        "decide_stall_redispatch",
        "apply_stall_redispatch_decision",
    }
)


def _stall(
    *,
    digest: str | None = "abc123",
    subtask_id: str = "s1",
) -> StallSignal:
    return StallSignal(
        loop_detected=True,
        stalled_streak=2,
        reason="no new evidence",
        error_signature="boom",
        subtask_id=subtask_id,
        contract_digest=digest,
    )


def test_flag_still_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STALL_REDISPATCH_ENFORCE_ENV, raising=False)
    assert stall_redispatch_enforce_enabled() is False


def test_select_alternate_agent_skips_current() -> None:
    assert select_alternate_agent("grok-build", ["grok-build", "codex", "opencode"]) == "codex"
    assert select_alternate_agent("codex", ["grok-build", "codex"]) == "grok-build"
    assert select_alternate_agent("only", ["only"]) is None
    assert select_alternate_agent("x", []) is None


def test_decide_noop_when_flag_off_or_no_stall() -> None:
    episode = StallEpisodeState()
    stall = _stall()
    assert (
        decide_stall_redispatch(
            enforce=False,
            stall=stall,
            episode=episode,
            current_agent="grok-build",
            available=["grok-build", "codex"],
        ).action
        == "noop"
    )
    assert (
        decide_stall_redispatch(
            enforce=True,
            stall=None,
            episode=episode,
            current_agent="grok-build",
            available=["grok-build", "codex"],
        ).action
        == "noop"
    )


def test_decide_swap_once_on_first_stall() -> None:
    decision = decide_stall_redispatch(
        enforce=True,
        stall=_stall(digest="deadbeef"),
        episode=StallEpisodeState(contract_digest="deadbeef"),
        current_agent="grok-build",
        available=["grok-build", "codex", "opencode"],
        expected_digest="deadbeef",
    )
    assert decision.action == "swap_once"
    assert decision.alternate_agent == "codex"


def test_decide_escalate_when_quota_used_or_no_alternate() -> None:
    used = StallEpisodeState(swaps_used=1, last_agent="codex")
    assert (
        decide_stall_redispatch(
            enforce=True,
            stall=_stall(),
            episode=used,
            current_agent="codex",
            available=["grok-build", "codex"],
        ).action
        == "escalate"
    )
    assert (
        decide_stall_redispatch(
            enforce=True,
            stall=_stall(),
            episode=StallEpisodeState(),
            current_agent="solo",
            available=["solo"],
        ).action
        == "escalate"
    )


def test_decide_escalate_on_digest_mismatch() -> None:
    decision = decide_stall_redispatch(
        enforce=True,
        stall=_stall(digest="aaa"),
        episode=StallEpisodeState(),
        current_agent="grok-build",
        available=["grok-build", "codex"],
        expected_digest="bbb",
    )
    assert decision.action == "escalate"
    assert "digest" in decision.reason.lower()


def test_apply_swap_once_preserves_user_request_and_stamps_agent() -> None:
    goal = "Ship the feature without touching main"
    digest = contract_digest(goal)
    subtask = Subtask(
        id="s1",
        parent_id="t1",
        description="impl",
        user_request=goal,
        status=SubtaskStatus.FAILED,
        agent_config={"agent": "grok-build"},
        dispatch_retry_count=0,
        result=SubtaskResult(subtask_id="s1", success=False),
    )
    episode = StallEpisodeState(contract_digest=digest)
    decision = RedispatchDecision(
        action="swap_once",
        alternate_agent="codex",
        reason="test swap",
    )
    new_ep = apply_stall_redispatch_decision(subtask, decision, episode)
    assert subtask.status == SubtaskStatus.PENDING
    assert subtask.user_request == goal
    assert contract_digest(subtask.user_request) == digest
    assert subtask.agent_config["agent"] == "codex"
    assert subtask.dispatch_retry_count == 1
    assert subtask.retry_count == 0
    assert subtask.result is None
    assert new_ep.swaps_used == 1
    assert new_ep.last_agent == "codex"
    assert load_stall_episode(subtask.agent_config).swaps_used == 1
    assert STALL_EPISODE_KEY in subtask.agent_config


def test_maybe_apply_flag_off_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STALL_REDISPATCH_ENFORCE_ENV, raising=False)
    subtask = Subtask(
        id="s1",
        parent_id="t1",
        description="d",
        user_request="goal",
        status=SubtaskStatus.IN_PROGRESS,
        agent_config={"agent": "grok-build"},
    )
    before = dict(subtask.agent_config)
    decision = maybe_apply_stall_redispatch(
        subtask,
        stall=_stall(digest=contract_digest("goal")),
        available=["grok-build", "codex"],
    )
    assert decision.action == "noop"
    assert subtask.status == SubtaskStatus.IN_PROGRESS
    assert subtask.agent_config == before


def test_maybe_apply_swap_then_escalate() -> None:
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
    first = maybe_apply_stall_redispatch(
        subtask,
        stall=_stall(digest=digest),
        available=["grok-build", "codex"],
        enforce=True,
    )
    assert first.action == "swap_once"
    assert subtask.status == SubtaskStatus.PENDING
    assert subtask.agent_config["agent"] == "codex"
    assert subtask.user_request == goal

    # Simulate the redispatch attempt stalling again under the alternate.
    subtask.status = SubtaskStatus.IN_PROGRESS
    second = maybe_apply_stall_redispatch(
        subtask,
        stall=_stall(digest=digest),
        available=["grok-build", "codex"],
        enforce=True,
        current_agent="codex",
    )
    assert second.action == "escalate"
    assert subtask.user_request == goal
    assert contract_digest(subtask.user_request) == digest


def test_stall_signal_from_mapping_roundtrip() -> None:
    original = _stall()
    parsed = stall_signal_from_mapping(original.to_dict())
    assert parsed is not None
    assert parsed.error_signature == original.error_signature
    assert parsed.contract_digest == original.contract_digest
    assert stall_signal_from_mapping(None) is None
    assert stall_signal_from_mapping({"loop_detected": True}) is None


def test_current_agent_from_config_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_CODING_AGENT", raising=False)
    assert current_agent_from_config({"agent": "codex", "agent_name": "x"}) == "codex"
    assert current_agent_from_config({"agent_name": "opencode"}) == "opencode"
    assert current_agent_from_config({}) == "grok-build"


def test_progress_still_has_no_replan_api() -> None:
    public = {name for name in dir(progress_mod) if not name.startswith("_")}
    assert public & _FORBIDDEN_PROGRESS_NAMES == set()
    source_path = Path(inspect.getsourcefile(progress_mod) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert defined & _FORBIDDEN_PROGRESS_NAMES == set()


@pytest.mark.asyncio
async def test_orchestrator_flag_off_no_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STALL_REDISPATCH_ENFORCE_ENV, raising=False)
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

    stall = build_stall_signal(
        ProgressVerdict("stop", 2, "stalled", "sig"),
        subtask_id="s1",
        contract_digest=contract_digest("keep digest"),
    )
    assert stall is not None
    result = SubtaskResult(
        subtask_id="s1",
        worker_id="w1",
        success=False,
        retryable=False,
        summary="stalled",
        stall_signal=stall.to_dict(),
    )
    await orch.handle_subtask_result(result)
    assert st.status == SubtaskStatus.FAILED
    assert st.agent_config.get("agent") == "grok-build"
    assert load_stall_episode(st.agent_config).swaps_used == 0


@pytest.mark.asyncio
async def test_orchestrator_swap_once_then_escalate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STALL_REDISPATCH_ENFORCE_ENV, "1")
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

    stall = StallSignal(
        loop_detected=True,
        stalled_streak=2,
        reason="stalled",
        error_signature="sig",
        subtask_id="s1",
        contract_digest=digest,
    )
    first = SubtaskResult(
        subtask_id="s1",
        worker_id="w1",
        success=False,
        retryable=False,
        summary="stall 1",
        stall_signal=stall.to_dict(),
    )
    await orch.handle_subtask_result(first)
    assert st.status == SubtaskStatus.PENDING
    assert st.agent_config["agent"] == "codex"
    assert st.user_request == goal
    assert contract_digest(st.user_request) == digest
    assert st.dispatch_retry_count == 1
    assert load_stall_episode(st.agent_config).swaps_used == 1
    assert task.status == TaskStatus.IN_PROGRESS

    # Second stall under the alternate → escalate to Failed.
    st.status = SubtaskStatus.IN_PROGRESS
    st.assigned_worker = "w1"
    orch.workers["w1"].current_load = 1
    second = SubtaskResult(
        subtask_id="s1",
        worker_id="w1",
        success=False,
        retryable=True,  # should be forced False on escalate
        summary="stall 2",
        stall_signal=stall.to_dict(),
    )
    await orch.handle_subtask_result(second)
    assert st.status == SubtaskStatus.FAILED
    assert st.user_request == goal
    assert contract_digest(st.user_request) == digest
    assert second.retryable is False
    assert st.agent_config["agent"] == "codex"  # no further swap
    assert load_stall_episode(st.agent_config).swaps_used == 1
