"""Progress detection: identical retries are stopped, moving ones are not."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.progress import (
    AttemptEvidence,
    ProgressTracker,
    signature,
)
from ultimate_coders.agent.types import Subtask, SubtaskResult
from ultimate_coders.agent.worker import Worker


def _failure(error: str, files: tuple[str, ...] = (), attempt: int = 0) -> AttemptEvidence:
    return AttemptEvidence(
        attempt=attempt,
        success=False,
        error_signature=signature(error),
        files=files,
    )


# ── signatures ─────────────────────────────────────────────────────


def test_signature_collapses_numbers_hex_ansi_and_whitespace():
    a = signature("Timeout after 600s\n  in  worker.py")
    b = signature("Timeout after 601s\r\n in  worker.py")
    assert a == b
    assert signature("\x1b[31mERROR\x1b[0m code") == "error code"
    assert signature("") == ""


# ── tracker ────────────────────────────────────────────────────────


def test_first_attempt_always_retries():
    tracker = ProgressTracker(stall_limit=2)
    verdict = tracker.observe(_failure("boom"))
    assert verdict.action == "retry" and verdict.stalled_streak == 0


def test_repeating_the_same_failure_stops_after_the_limit():
    tracker = ProgressTracker(stall_limit=2)
    assert tracker.observe(_failure("boom")).action == "retry"  # first
    assert tracker.observe(_failure("boom")).action == "retry"  # stall 1
    stopped = tracker.observe(_failure("boom"))  # stall 2
    assert stopped.action == "stop"
    assert stopped.stalled_streak == 2
    assert "no new evidence" in stopped.reason


def test_changed_error_resets_the_streak():
    tracker = ProgressTracker(stall_limit=2)
    tracker.observe(_failure("boom"))
    tracker.observe(_failure("boom"))
    moved = tracker.observe(_failure("different failure"))
    assert moved.action == "retry" and moved.stalled_streak == 0


def test_changed_file_set_counts_as_new_evidence():
    tracker = ProgressTracker(stall_limit=2)
    tracker.observe(_failure("boom", files=("a.py",)))
    moved = tracker.observe(_failure("boom", files=("b.py",)))
    assert moved.stalled_streak == 0


def test_success_is_never_a_stall():
    tracker = ProgressTracker(stall_limit=1)
    ok = AttemptEvidence(attempt=1, success=True)
    assert tracker.observe(ok).action == "retry"
    assert tracker.observe(ok).stalled_streak == 0


def test_stall_limit_one_stops_on_first_repeat():
    tracker = ProgressTracker(stall_limit=1)
    tracker.observe(_failure("boom"))
    assert tracker.observe(_failure("boom")).action == "stop"


# ── worker integration ─────────────────────────────────────────────


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
async def test_worker_stops_identical_retries_before_the_retry_budget():
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

    # MAX_RETRIES is 5; the loop stops on the second no-evidence repeat.
    assert len(executions) == 3
    assert result.success is False
    assert result.retryable is False
    assert "retry loop stopped" in result.summary
    final = [d for t, d in events if t == "subtask_failed"][-1]
    assert final["loop_detected"] is True
    assert final["progress"]["action"] == "stop"
    assert final["progress"]["stalled_streak"] == 2


@pytest.mark.asyncio
async def test_worker_keeps_retrying_while_the_failure_moves():
    executions: list[int] = []
    events: list[tuple[str, dict]] = []
    # Genuinely different failures. A bare counter would NOT move the
    # signature -- digits collapse by design -- which is itself the point:
    # "attempt 3" is not new information.
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
    result = await worker.execute_subtask(Subtask(id="s1", parent_id="t1", description="d"))

    assert len(executions) == 5  # full budget: every failure differed
    assert "loop_detected" not in [d for t, d in events if t == "subtask_failed"][-1]
    assert result.retryable is True
