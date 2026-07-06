"""Tests for LLM error classification and Worker friendly error messages.

Covers:
- _classify_llm_error: 503/429/400/401/unknown classification
- LLMRetryExhaustedError: carries classification + original exception
- Worker._build_friendly_error: transient/permanent/unknown friendly summaries
- Worker.execute_subtask: sets SubtaskResult.error on failure paths
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from ultimate_coders.agent.llm import (
    LLMErrorClassification,
    LLMRetryExhaustedError,
    _classify_llm_error,
)
from ultimate_coders.agent.types import Subtask, SubtaskResult
from ultimate_coders.agent.worker import Worker, _build_friendly_error

# ── _classify_llm_error ────────────────────────────────────────


class TestClassifyLlmError:
    def test_classify_503_as_transient(self) -> None:
        cls = _classify_llm_error(Exception("503 The system is busy, try again later"))
        assert cls.kind == "transient"
        assert cls.retry_count == 0
        assert "503" in cls.message

    def test_classify_429_as_transient(self) -> None:
        cls = _classify_llm_error(Exception("429 rate_limit exceeded"))
        assert cls.kind == "transient"

    def test_classify_529_overloaded_as_transient(self) -> None:
        cls = _classify_llm_error(Exception("529 overloaded"))
        assert cls.kind == "transient"

    def test_classify_server_error_as_transient(self) -> None:
        cls = _classify_llm_error(Exception("Internal server_error"))
        assert cls.kind == "transient"

    def test_classify_400_as_permanent(self) -> None:
        cls = _classify_llm_error(Exception("400 bad request"))
        assert cls.kind == "permanent"

    def test_classify_401_as_permanent(self) -> None:
        cls = _classify_llm_error(Exception("401 invalid_api_key"))
        assert cls.kind == "permanent"

    def test_classify_403_forbidden_as_permanent(self) -> None:
        cls = _classify_llm_error(Exception("403 forbidden"))
        assert cls.kind == "permanent"

    def test_classify_unknown_error(self) -> None:
        cls = _classify_llm_error(Exception("Something weird happened"))
        assert cls.kind == "unknown"

    def test_classify_preserves_retry_count(self) -> None:
        cls = _classify_llm_error(Exception("503 busy"), retry_count=5)
        assert cls.retry_count == 5

    def test_classify_accepts_string_input(self) -> None:
        cls = _classify_llm_error("429 rate limit", retry_count=3)
        assert cls.kind == "transient"
        assert cls.retry_count == 3


# ── LLMRetryExhaustedError ─────────────────────────────────────


class TestLLMRetryExhaustedError:
    def test_carries_original_and_classification(self) -> None:
        original = Exception("503 system is busy")
        cls = LLMErrorClassification(kind="transient", retry_count=5, message="503 system is busy")
        wrapped = LLMRetryExhaustedError(original, cls)
        assert wrapped.original is original
        assert wrapped.classification is cls
        assert "503" in str(wrapped)

    def test_is_runtime_error(self) -> None:
        err = LLMRetryExhaustedError(
            Exception("401"),
            LLMErrorClassification(kind="permanent", retry_count=0, message="401"),
        )
        assert isinstance(err, RuntimeError)


# ── _build_friendly_error ──────────────────────────────────────


class TestBuildFriendlyError:
    def test_transient_llm_error_summary(self) -> None:
        original = Exception("503 The system is busy, try again later")
        cls = LLMErrorClassification(
            kind="transient", retry_count=5, message="503 The system is busy, try again later",
        )
        wrapped = LLMRetryExhaustedError(original, cls)
        summary, error = _build_friendly_error(wrapped)
        assert "瞬时错误" in summary
        assert "5" in summary  # retry count
        assert "503" in summary
        assert "503" in error

    def test_permanent_llm_error_summary(self) -> None:
        original = Exception("401 invalid_api_key")
        cls = LLMErrorClassification(kind="permanent", retry_count=0, message="401 invalid_api_key")
        wrapped = LLMRetryExhaustedError(original, cls)
        summary, error = _build_friendly_error(wrapped)
        assert "永久错误" in summary
        assert "401" in summary
        assert "401" in error
        # Permanent errors should NOT show retry count
        assert "重试" not in summary

    def test_unknown_llm_error_summary(self) -> None:
        original = Exception("Something unexpected")
        cls = LLMErrorClassification(kind="unknown", retry_count=2, message="Something unexpected")
        wrapped = LLMRetryExhaustedError(original, cls)
        summary, error = _build_friendly_error(wrapped)
        assert "LLM" in summary
        assert "2" in summary  # retry count for unknown
        assert "Something unexpected" in error

    def test_non_llm_exception_falls_back(self) -> None:
        e = RuntimeError("503 service unavailable")
        summary, error = _build_friendly_error(e)
        assert "瞬时错误" in summary
        assert "503" in error

    def test_non_llm_permanent_exception(self) -> None:
        e = RuntimeError("401 unauthorized access")
        summary, error = _build_friendly_error(e)
        assert "永久错误" in summary

    def test_non_llm_unknown_exception(self) -> None:
        e = RuntimeError("disk full on /tmp")
        summary, error = _build_friendly_error(e)
        assert "Execution error" in summary
        assert "disk full" in error


# ── Worker sets SubtaskResult.error ────────────────────────────


class TestWorkerSetsErrorField:
    """Verify that Worker failure paths populate SubtaskResult.error."""

    def _make_worker(self, stub_engine) -> Worker:
        from ultimate_coders.agent.sandbox import SandboxConfig
        # engine=None to avoid auto-MCP registration (faster, no deps)
        return Worker(engine=None, sandbox_config=SandboxConfig())

    def _make_subtask(self) -> Subtask:
        return Subtask(
            id="st-test-001",
            parent_id="task-001",
            description="Test subtask",
        )

    def test_exception_path_sets_error_field(self, stub_engine) -> None:
        """When execute_subtask catches an exception, SubtaskResult.error is set."""
        worker = self._make_worker(stub_engine)
        subtask = self._make_subtask()

        # Patch _execute_in_sandbox to raise LLMRetryExhaustedError (transient, exhausted)
        original_err = Exception("503 The system is busy, try again later")
        cls = LLMErrorClassification(
            kind="transient", retry_count=3, message="503 The system is busy, try again later",
        )
        llm_err = LLMRetryExhaustedError(original_err, cls)

        async def _raise(*args, **kwargs):
            raise llm_err

        with patch.object(worker, "_execute_in_sandbox", side_effect=_raise):
            # Also patch _publish_event to avoid needing NATS/event_emitter
            async def _noop_pub(*args, **kwargs):
                pass
            worker._publish_event = _noop_pub  # type: ignore[assignment]

            # Patch sleep to make retry delays instant
            with patch(
                "ultimate_coders.agent.worker.asyncio.sleep",
                new_callable=MagicMock,
            ) as mock_sleep:
                async def _instant_sleep(*a, **kw):
                    pass
                mock_sleep.side_effect = _instant_sleep

                result = asyncio.run(worker.execute_subtask(subtask))

        assert result.success is False
        # error field must be set (this is the bug fix)
        assert result.error != ""
        assert "503" in result.error
        # summary must be friendly
        assert "瞬时错误" in result.summary
        assert "3" in result.summary  # retry count

    def test_exception_path_permanent_error_sets_error(self, stub_engine) -> None:
        """Permanent LLM errors set error field and show 永久错误."""
        worker = self._make_worker(stub_engine)
        subtask = self._make_subtask()

        original_err = Exception("401 invalid_api_key")
        cls = LLMErrorClassification(kind="permanent", retry_count=0, message="401 invalid_api_key")
        llm_err = LLMRetryExhaustedError(original_err, cls)

        async def _raise(*args, **kwargs):
            raise llm_err

        with patch.object(worker, "_execute_in_sandbox", side_effect=_raise):
            async def _noop_pub(*args, **kwargs):
                pass
            worker._publish_event = _noop_pub  # type: ignore[assignment]

            with patch(
                "ultimate_coders.agent.worker.asyncio.sleep",
                new_callable=MagicMock,
            ) as mock_sleep:
                async def _instant_sleep(*a, **kw):
                    pass
                mock_sleep.side_effect = _instant_sleep

                result = asyncio.run(worker.execute_subtask(subtask))

        assert result.success is False
        assert result.error != ""
        assert "401" in result.error
        assert "永久错误" in result.summary

    def test_sandbox_failure_sets_error_field(self, stub_engine) -> None:
        """When _execute_in_sandbox returns success=False, error field is set."""
        worker = self._make_worker(stub_engine)
        subtask = self._make_subtask()

        # Patch _execute_in_sandbox to return a failed SubtaskResult
        async def _fail_sandbox(*args, **kwargs):
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=worker.worker_id,
                summary="Build failed: syntax error in main.py",
                success=False,
                error="Build failed: syntax error in main.py",
                stderr_tail="SyntaxError: invalid syntax",
            )

        with patch.object(worker, "_execute_in_sandbox", side_effect=_fail_sandbox):
            async def _noop_pub(*args, **kwargs):
                pass
            worker._publish_event = _noop_pub  # type: ignore[assignment]

            with patch(
                "ultimate_coders.agent.worker.asyncio.sleep",
                new_callable=MagicMock,
            ) as mock_sleep:
                async def _instant_sleep(*a, **kw):
                    pass
                mock_sleep.side_effect = _instant_sleep

                result = asyncio.run(worker.execute_subtask(subtask))

        assert result.success is False
        # error field should be set to the summary (root cause)
        assert result.error != ""
        assert "Build failed" in result.error

    def test_timeout_sets_error_field(self, stub_engine) -> None:
        """Timeout failure path sets error field."""
        worker = self._make_worker(stub_engine)
        # Override MAX_RETRIES to 1 so there's no retry delay to wait through
        worker.MAX_RETRIES = 1
        subtask = self._make_subtask()
        subtask.timeout_seconds = 1  # Very short timeout

        async def _slow_sandbox(*args, **kwargs):
            # Real sleep that exceeds the timeout — wait_for will cancel this
            await asyncio.sleep(5)
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=worker.worker_id,
                summary="done",
                success=True,
            )

        with patch.object(worker, "_execute_in_sandbox", side_effect=_slow_sandbox):
            async def _noop_pub(*args, **kwargs):
                pass
            worker._publish_event = _noop_pub  # type: ignore[assignment]

            result = asyncio.run(worker.execute_subtask(subtask))

        assert result.success is False
        assert result.error != ""
        assert "timed out" in result.error.lower()

    def test_success_does_not_set_error(self, stub_engine) -> None:
        """Successful execution should NOT set error field (stays empty)."""
        worker = self._make_worker(stub_engine)
        subtask = self._make_subtask()

        async def _success_sandbox(*args, **kwargs):
            return SubtaskResult(
                subtask_id=subtask.id,
                worker_id=worker.worker_id,
                summary="Task completed successfully",
                success=True,
            )

        with patch.object(worker, "_execute_in_sandbox", side_effect=_success_sandbox):
            async def _noop_pub(*args, **kwargs):
                pass
            worker._publish_event = _noop_pub  # type: ignore[assignment]

            result = asyncio.run(worker.execute_subtask(subtask))

        assert result.success is True
        assert result.error == ""  # No error on success


# ── _parse_litellm_response empty-choices guard ─────────────────


class TestParseLitellmEmptyChoices:
    """Regression: bare `response.choices[0]` raised IndexError on empty
    choices (some providers return empty choices on content-filter/error),
    misclassified as an unknown LLM error."""

    def test_empty_choices_returns_empty_response_not_indexerror(self) -> None:
        from ultimate_coders.agent.llm import LLMClient

        client = LLMClient()
        # Simulate a provider response with no choices.
        resp = MagicMock()
        resp.choices = []
        resp.model = "gpt-4o"

        out = client._parse_litellm_response(resp)
        assert out.text == ""
        assert out.tool_calls == []
        assert out.stop_reason == "empty_choices"

    def test_none_choices_returns_empty_response(self) -> None:
        from ultimate_coders.agent.llm import LLMClient

        client = LLMClient()
        resp = MagicMock()
        resp.choices = None  # type: ignore[assignment]

        out = client._parse_litellm_response(resp)
        assert out.stop_reason == "empty_choices"

