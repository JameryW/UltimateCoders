"""ExecutionScope tests (T8 #650 / D8 #645).

Covers the Python side of scope enforcement:
- ``NatsWorker._worker_projects`` — UC_WORKER_PROJECTS parsing (trim, blanks
  dropped, dedupe, semicolon tolerated).
- ``NatsWorker._register_with_gateway`` — projects threaded to the gRPC
  register RPC (open worker = empty list, scoped = parsed env).
- ``NatsWorker._handle_submit`` — empty/whitespace project_id on
  ``uc.task.submit`` is rejected before Orchestrator.submit_task.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from ultimate_coders.agent.orchestrator import Orchestrator

# ── helpers ──────────────────────────────────────────────────────────


def _llm_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    return resp


def _subtask_json_list(items: list[dict]) -> str:
    return json.dumps(items)


def _st(desc: str) -> dict:
    return {
        "description": desc,
        "depends_on": [],
        "file_constraints": [],
        "expected_output": "",
    }


def _make_orchestrator() -> Orchestrator:
    llm = MagicMock()
    llm.complete = AsyncMock(
        return_value=_llm_response(_subtask_json_list([_st("Do the thing")]))
    )
    return Orchestrator(llm_client=llm)


def _make_handle_submit_worker() -> MagicMock:
    """NatsWorker-like mock with just enough for _handle_submit (mirrors
    the pattern in test_night_window_exclusive.py)."""
    worker = MagicMock()
    worker._orchestrator = _make_orchestrator()
    worker._cancelled_task_ids = set()

    def _consume_bg(coro):
        coro.close()

    worker._spawn_bg = MagicMock(side_effect=_consume_bg)

    async def _dummy_exec(task):
        pass

    worker._execute_subtasks = lambda task: _dummy_exec(task)
    return worker


def _make_msg(payload: dict) -> MagicMock:
    msg = MagicMock()
    msg.data = json.dumps(payload).encode("utf-8")
    msg.reply = None
    return msg


# ── UC_WORKER_PROJECTS parsing ───────────────────────────────────────


class TestWorkerProjects:
    def test_unset_env_means_open_worker(self, monkeypatch):
        from ultimate_coders.nats_worker import NatsWorker

        monkeypatch.delenv("UC_WORKER_PROJECTS", raising=False)
        worker = MagicMock()
        assert NatsWorker._worker_projects(worker) == []

    def test_empty_env_means_open_worker(self, monkeypatch):
        from ultimate_coders.nats_worker import NatsWorker

        monkeypatch.setenv("UC_WORKER_PROJECTS", "")
        worker = MagicMock()
        assert NatsWorker._worker_projects(worker) == []

    def test_comma_semicolon_split_trim_and_dedupe(self, monkeypatch):
        from ultimate_coders.nats_worker import NatsWorker

        monkeypatch.setenv("UC_WORKER_PROJECTS", " alpha , beta ;alpha; ;gamma")
        worker = MagicMock()
        assert NatsWorker._worker_projects(worker) == ["alpha", "beta", "gamma"]


# ── registration passthrough ─────────────────────────────────────────


class TestRegisterWithGatewayProjects:
    async def test_scoped_registration_threads_projects(self, monkeypatch):
        import ultimate_coders.nats_worker as nw

        monkeypatch.setenv("UC_WORKER_PROJECTS", "alpha, beta")

        captured: dict = {}

        class _FakeGrpcEngine:
            def __init__(self, **kwargs):
                pass

            async def register_worker_async(
                self,
                worker_id,
                capabilities,
                max_capacity,
                metadata,
                contract_version,
                projects,
            ):
                captured["projects"] = projects
                captured["worker_id"] = worker_id
                return True

        monkeypatch.setattr(nw, "Engine", _FakeGrpcEngine)

        worker = MagicMock()
        worker._mode = "worker"
        worker._subtask_js_available = True
        worker._grpc_endpoint = "http://localhost:50051"
        worker._worker = None
        worker._consumer_id = "w-1"
        # Bind the real env-parsing method so the test covers env → RPC.
        worker._worker_projects = lambda: nw.NatsWorker._worker_projects(worker)
        worker._registration_metadata = lambda: "{}"

        await nw.NatsWorker._register_with_gateway(worker)

        assert captured["projects"] == ["alpha", "beta"]
        assert captured["worker_id"] == "w-1"

    async def test_open_worker_registers_with_empty_projects(self, monkeypatch):
        import ultimate_coders.nats_worker as nw

        monkeypatch.delenv("UC_WORKER_PROJECTS", raising=False)

        captured: dict = {}

        class _FakeGrpcEngine:
            def __init__(self, **kwargs):
                pass

            async def register_worker_async(
                self,
                worker_id,
                capabilities,
                max_capacity,
                metadata,
                contract_version,
                projects,
            ):
                captured["projects"] = projects
                return True

        monkeypatch.setattr(nw, "Engine", _FakeGrpcEngine)

        worker = MagicMock()
        worker._mode = "worker"
        worker._subtask_js_available = True
        worker._grpc_endpoint = "http://localhost:50051"
        worker._worker = None
        worker._consumer_id = "w-1"
        worker._worker_projects = lambda: nw.NatsWorker._worker_projects(worker)
        worker._registration_metadata = lambda: "{}"

        await nw.NatsWorker._register_with_gateway(worker)

        assert captured["projects"] == []


# ── uc.task.submit scope validation ──────────────────────────────────


class TestHandleSubmitScopeValidation:
    async def test_empty_project_id_rejected(self):
        from ultimate_coders.nats_worker import NatsWorker

        worker = _make_handle_submit_worker()
        msg = _make_msg(
            {
                "task_id": "t-noscope",
                "description": "No scope",
                "project_id": "",
            }
        )

        await NatsWorker._handle_submit(worker, msg)

        assert len(worker._orchestrator._pending_tasks) == 0
        worker._spawn_bg.assert_not_called()

    async def test_whitespace_project_id_rejected(self):
        from ultimate_coders.nats_worker import NatsWorker

        worker = _make_handle_submit_worker()
        msg = _make_msg(
            {
                "task_id": "t-blank",
                "description": "Blank scope",
                "project_id": "   ",
            }
        )

        await NatsWorker._handle_submit(worker, msg)

        assert len(worker._orchestrator._pending_tasks) == 0
        worker._spawn_bg.assert_not_called()

    async def test_missing_project_id_rejected(self):
        from ultimate_coders.nats_worker import NatsWorker

        worker = _make_handle_submit_worker()
        msg = _make_msg(
            {
                "task_id": "t-missing",
                "description": "Missing scope",
            }
        )

        await NatsWorker._handle_submit(worker, msg)

        assert len(worker._orchestrator._pending_tasks) == 0

    async def test_concrete_project_id_still_submitted(self):
        """Sanity control: a valid scope flows through unchanged."""
        from ultimate_coders.nats_worker import NatsWorker

        worker = _make_handle_submit_worker()
        msg = _make_msg(
            {
                "task_id": "t-scoped",
                "description": "Scoped task",
                "project_id": "proj",
            }
        )

        await NatsWorker._handle_submit(worker, msg)

        # Night-window inactive → task runs immediately (not deferred).
        assert len(worker._orchestrator._pending_tasks) == 0
        worker._spawn_bg.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
