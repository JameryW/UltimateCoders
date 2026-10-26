"""Stall signal surface contracts (T13 / D7 observe half).

Guards: UC_STALL_REDISPATCH_ENFORCE default off; build_stall_signal on stop vs
retry; to_dict shape; worker attaches stall_signal without inventing replan in
progress.py.
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
from ultimate_coders.agent.progress import (  # noqa: E402
    AttemptEvidence,
    ProgressTracker,
    ProgressVerdict,
    signature,
)
from ultimate_coders.agent.stall_redispatch import (  # noqa: E402
    STALL_REDISPATCH_ENFORCE_ENV,
    StallSignal,
    build_stall_signal,
    stall_redispatch_enforce_enabled,
)
from ultimate_coders.agent.types import Subtask, SubtaskResult  # noqa: E402
from ultimate_coders.agent.worker import Worker  # noqa: E402

# Names that would mean progress.py grew a replan / agent-swap primitive.
_FORBIDDEN_PROGRESS_NAMES = frozenset(
    {
        "replan",
        "redispatch",
        "swap_agent",
        "agent_swap",
        "alternate_agent",
        "stall_redispatch_enforce_enabled",
    }
)


def test_stall_redispatch_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STALL_REDISPATCH_ENFORCE_ENV, raising=False)
    assert stall_redispatch_enforce_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_stall_redispatch_flag_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(STALL_REDISPATCH_ENFORCE_ENV, value)
    assert stall_redispatch_enforce_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "maybe", ""])
def test_stall_redispatch_flag_falsey(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(STALL_REDISPATCH_ENFORCE_ENV, value)
    assert stall_redispatch_enforce_enabled() is False


def test_build_stall_signal_on_stop() -> None:
    verdict = ProgressVerdict(
        "stop",
        2,
        "no new evidence: same error signature, stderr and file set",
        "undefined symbol foo",
    )
    signal = build_stall_signal(
        verdict,
        subtask_id="s1",
        contract_digest="deadbeef",
    )
    assert signal is not None
    assert signal.loop_detected is True
    assert signal.stalled_streak == 2
    assert "no new evidence" in signal.reason
    assert signal.error_signature == "undefined symbol foo"
    assert signal.subtask_id == "s1"
    assert signal.contract_digest == "deadbeef"


def test_build_stall_signal_none_on_retry() -> None:
    verdict = ProgressVerdict("retry", 1, "no new evidence", "boom")
    assert build_stall_signal(verdict, subtask_id="s1") is None


def test_stall_signal_to_dict_shape() -> None:
    signal = StallSignal(
        loop_detected=True,
        stalled_streak=2,
        reason="stalled",
        error_signature="boom",
        subtask_id="s1",
    )
    payload = signal.to_dict()
    assert payload == {
        "loop_detected": True,
        "stalled_streak": 2,
        "reason": "stalled",
        "error_signature": "boom",
        "subtask_id": "s1",
    }
    # Optional contract_digest omitted when unset.
    assert "contract_digest" not in payload

    with_digest = StallSignal(
        loop_detected=True,
        stalled_streak=1,
        reason="r",
        error_signature="e",
        contract_digest="abc",
    ).to_dict()
    assert with_digest["contract_digest"] == "abc"
    assert "subtask_id" not in with_digest


def test_flag_on_still_does_not_swap_via_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T13 observe-only: enforce on must not change build_stall_signal."""
    monkeypatch.setenv(STALL_REDISPATCH_ENFORCE_ENV, "1")
    assert stall_redispatch_enforce_enabled() is True
    stop = ProgressVerdict("stop", 2, "stalled", "sig")
    retry = ProgressVerdict("retry", 0, "first attempt", "sig")
    assert build_stall_signal(stop) is not None
    assert build_stall_signal(retry) is None


def test_progress_module_has_no_replan_or_swap_api() -> None:
    """progress.py stays evidence-only; no replan/swap callable may land there.

    Docstring prose may say "no replan primitive" — that is allowed. Inventing
    a callable or ProgressVerdict action for replan/redispatch/swap is not.
    """
    public = {name for name in dir(progress_mod) if not name.startswith("_")}
    leaked = public & _FORBIDDEN_PROGRESS_NAMES
    assert leaked == set(), f"progress.py grew forbidden public names: {leaked}"

    source_path = Path(inspect.getsourcefile(progress_mod) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    leaked_defs = defined & _FORBIDDEN_PROGRESS_NAMES
    assert leaked_defs == set(), f"progress.py defined forbidden APIs: {leaked_defs}"

    # Verdict actions stay retry|stop only (no replan/redispatch action).
    tracker = ProgressTracker(stall_limit=1)
    first = tracker.observe(AttemptEvidence(0, False, error_signature="a"))
    second = tracker.observe(AttemptEvidence(1, False, error_signature="a"))
    assert first.action == "retry"
    assert second.action == "stop"
    assert {first.action, second.action} <= {"retry", "stop"}


class _LoopWorker(Worker):
    MAX_RETRIES = 5
    RETRY_DELAYS = [0.0, 0.0, 0.0, 0.0]


def _worker(executions: list[int], events: list[tuple[str, dict]], behaviour) -> _LoopWorker:
    worker = _LoopWorker(worker_id="w", engine=None)

    async def _fake(subtask, context_block, workspace_handle):
        executions.append(1)
        return behaviour(len(executions))

    async def _event(event_type, **kw):
        events.append((event_type, kw.get("data", {})))

    worker._execute_in_sandbox = _fake  # type: ignore[assignment]
    worker._publish_event = _event  # type: ignore[assignment]
    return worker


@pytest.mark.asyncio
async def test_worker_attaches_stall_signal_on_progress_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(STALL_REDISPATCH_ENFORCE_ENV, raising=False)
    executions: list[int] = []
    events: list[tuple[str, dict]] = []

    def behaviour(_n):
        return SubtaskResult(
            subtask_id="s1",
            success=False,
            retryable=True,
            summary="undefined symbol foo",
            error="undefined symbol foo",
        )

    worker = _worker(executions, events, behaviour)
    result = await worker.execute_subtask(Subtask(id="s1", parent_id="t1", description="d"))

    assert result.success is False
    assert result.retryable is False
    final = [d for t, d in events if t == "subtask_failed"][-1]
    assert final["loop_detected"] is True
    assert final["progress"]["action"] == "stop"
    stall = final["stall_signal"]
    assert stall["loop_detected"] is True
    assert stall["stalled_streak"] == 2
    assert "no new evidence" in stall["reason"]
    assert stall["error_signature"] == signature("undefined symbol foo")
    assert stall["subtask_id"] == "s1"
    # Flag off: still stop-without-redispatch (no swap metadata invented).
    assert "redispatch" not in final
    assert "alternate_agent" not in final


@pytest.mark.asyncio
async def test_worker_no_stall_signal_while_failure_moves() -> None:
    executions: list[int] = []
    events: list[tuple[str, dict]] = []
    variants = [
        "undefined symbol foo",
        "missing include bar.h",
        "linker error: baz",
        "type mismatch in qux",
        "permission denied: zap",
    ]

    def behaviour(n):
        text = variants[min(n, len(variants)) - 1]
        return SubtaskResult(
            subtask_id="s1", success=False, retryable=True, summary=text, error=text
        )

    worker = _worker(executions, events, behaviour)
    await worker.execute_subtask(Subtask(id="s1", parent_id="t1", description="d"))

    failed = [d for t, d in events if t == "subtask_failed"]
    assert failed
    assert "loop_detected" not in failed[-1]
    assert "stall_signal" not in failed[-1]


def test_tracker_stop_feeds_build_stall_signal() -> None:
    tracker = ProgressTracker(stall_limit=2)
    tracker.observe(AttemptEvidence(0, False, error_signature=signature("boom")))
    tracker.observe(AttemptEvidence(1, False, error_signature=signature("boom")))
    stopped = tracker.observe(AttemptEvidence(2, False, error_signature=signature("boom")))
    assert stopped.action == "stop"
    signal = build_stall_signal(stopped)
    assert signal is not None
    assert signal.stalled_streak == stopped.stalled_streak
    assert signal.error_signature == stopped.signature
