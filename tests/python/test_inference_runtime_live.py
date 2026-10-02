"""Opt-in confirmation test; use a private broker and Gateway/database."""

import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_terminal_outbox_is_durable_and_restart_safe(monkeypatch):
    url = os.environ.get("UC_RUNTIME_TEST_NATS_URL")
    database = os.environ.get("UC_RUNTIME_TEST_GATEWAY_DB")
    if not url or not database:
        pytest.skip("Requires private NATS/Gateway and UC_RUNTIME_TEST_GATEWAY_DB")
    import nats
    import psycopg
    from ultimate_coders.agent.orchestrator import Orchestrator
    from ultimate_coders.agent.types import Subtask, SubtaskResult, SubtaskStatus, Task, TaskStatus
    from ultimate_coders.nats_worker import NatsPublisher, NatsWorker
    from ultimate_coders.runtime_state import RuntimeState

    monkeypatch.setenv("UC_DATABASE_URL", database)
    nc = await nats.connect(url)
    subscription = None
    try:
        # Container start is asynchronous. Wait for the actual subscriber,
        # rather than interpreting a cold service as a protocol failure.
        for _ in range(100):
            try:
                await nc.request("uc.task.gateway-snapshot.request", b'{"task_id":"ready"}',
                                 timeout=1)
                break
            except (nats.errors.NoRespondersError, nats.errors.TimeoutError):
                await asyncio.sleep(0.2)
        else:
            pytest.fail("Private Gateway did not become ready")
        publisher = NatsPublisher(nc)
        task = Task(
            id="reliability-" + uuid.uuid4().hex,
            description="Isolated delivery fixture",
            project_id="fixture",
            status=TaskStatus.IN_PROGRESS,
        )
        node = Subtask(
            id=task.id + "-node",
            parent_id=task.id,
            status=SubtaskStatus.ASSIGNED,
            assigned_worker="fixture",
            dispatch_retry_count=0,
        )
        task.subtasks = [node]
        from ultimate_coders.nats_worker import _make_task_update_payload

        assert await publisher.confirm_update(_make_task_update_payload(task))

        def coordinator():
            worker = NatsWorker(mode="default")
            worker._orchestrator = Orchestrator(nats_publisher=publisher)
            worker._publisher, worker._nc = publisher, nc
            return worker

        owner = coordinator()  # Empty in-memory plan: recover from Gateway.
        subscription = await nc.subscribe("uc.task.event", cb=owner._handle_task_event)
        runner = NatsWorker(mode="worker")
        runner._publisher = publisher
        runner._worker = SimpleNamespace(
            worker_id="fixture",
            execute_subtask=AsyncMock(
                return_value=SubtaskResult(subtask_id=node.id, summary="durable completion")
            ),
        )
        await runner._execute_and_report_body(node)
        state = RuntimeState()
        record = state.get("result_outbox", f"{task.id}:{node.id}:0")
        assert record["delivered"] is True
        with psycopg.connect(database) as connection:
            row = connection.execute(
                "SELECT status, subtasks FROM tasks WHERE id=%s", (task.id,)
            ).fetchone()
            assert row[0] == "Completed"
            assert row[1][0]["status"] == "Completed"
            assert row[1][0]["dispatch_retry_count"] == 0
        response = await nc.request(
            "uc.task.gateway-snapshot.request", json.dumps({"task_id": task.id}).encode()
        )
        assert json.loads(response.data)["task"]["status"] == "Completed"
        await subscription.unsubscribe()
        subscription = await nc.subscribe("uc.task.event", cb=coordinator()._handle_task_event)
        assert await publisher.publish_terminal(record["event"], record["update"])
        runner._worker.execute_subtask.assert_awaited_once()
    finally:
        if subscription:
            await subscription.unsubscribe()
        await nc.close()
