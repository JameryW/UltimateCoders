"""Historical Dashboard events come from the durable Gateway event stream."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from nats.js.errors import NotFoundError
from ultimate_coders.dashboard.app import DashboardApp
from ultimate_coders.dashboard.event_history import list_event_history
from ultimate_coders.nats_worker import NatsWorker

TASK_ID = "task-history"


class FakeSubscription:
    def __init__(self, messages):
        self.messages = messages
        self.unsubscribed = False

    async def consumer_info(self):
        return SimpleNamespace(name="temporary-history")

    async def fetch(self, *, batch, timeout):
        del batch, timeout
        if self.messages:
            messages, self.messages = self.messages, []
            return messages
        raise asyncio.TimeoutError

    async def unsubscribe(self):
        self.unsubscribed = True


class FakeJetStream:
    def __init__(self):
        self.subject = None
        self.subscription = None
        self.deleted = None

    async def pull_subscribe(self, subject, *, stream):
        assert stream == "AGENT_EVENTS"
        self.subject = subject
        messages = [
            SimpleNamespace(
                data=json.dumps(
                    {"TaskCreated": {"task_id": TASK_ID, "description": "work"}}
                ).encode(),
                metadata=SimpleNamespace(timestamp=datetime(2026, 9, 24, 1, tzinfo=timezone.utc)),
                subject=f"task.{TASK_ID}",
            ),
            SimpleNamespace(
                data=json.dumps(
                    {"SubtaskProgress": {
                        "task_id": TASK_ID,
                        "subtask_id": "s1",
                        "phase": "executing",
                        "percent": 50,
                    }}
                ).encode(),
                metadata=SimpleNamespace(timestamp=datetime(2026, 9, 24, 2, tzinfo=timezone.utc)),
                subject=f"task.{TASK_ID}",
            ),
        ]
        self.subscription = FakeSubscription(messages)
        return self.subscription

    async def delete_consumer(self, stream, name):
        self.deleted = (stream, name)


class FakeNats:
    def __init__(self):
        self.js = FakeJetStream()

    def jetstream(self):
        return self.js


class MissingHistoryNats(FakeNats):
    def __init__(self):
        super().__init__()

        async def missing_stream(subject, *, stream):
            del subject, stream
            raise NotFoundError()

        self.js.pull_subscribe = missing_stream


@pytest.mark.asyncio
async def test_history_is_newest_first_and_keeps_subtask_and_timestamp():
    nats = FakeNats()
    result = await list_event_history(nats, task_id=TASK_ID, limit=1, offset=0)

    assert nats.js.subject == f"task.{TASK_ID}"
    assert result["total"] == 2
    assert result["events"][0] == {
        "timestamp": "2026-09-24T02:00:00+00:00",
        "type": "subtask_progress",
        "task_id": TASK_ID,
        "subtask_id": "s1",
        "data": {"subtask_id": "s1", "phase": "executing", "percent": 50},
    }
    assert nats.js.subscription.unsubscribed
    assert nats.js.deleted == ("AGENT_EVENTS", "temporary-history")


@pytest.mark.asyncio
async def test_gateway_dashboard_rpc_reads_durable_history():
    worker = NatsWorker(project_path="/tmp/test", mode="default")
    worker._orchestrator = SimpleNamespace()
    worker._nc = FakeNats()

    result = await worker._dash_listevents({"task_id": TASK_ID, "limit": 100, "offset": 0})

    assert result["total"] == 2
    assert result["events"][0]["type"] == "subtask_progress"


def test_rest_events_read_durable_history():
    nats = FakeNats()
    app = DashboardApp(orchestrator=None, nats_client=nats)

    client = TestClient(app._app)
    result = client.get("/dashboard/api/events", params={"task_id": TASK_ID}).json()

    assert result["total"] == 2
    assert result["events"][0]["type"] == "subtask_progress"


def test_rest_events_fall_back_when_stream_is_not_configured():
    app = DashboardApp(orchestrator=None, nats_client=MissingHistoryNats())
    app._event_log.appendleft({"task_id": TASK_ID, "type": "local_event"})

    result = TestClient(app._app).get(
        "/dashboard/api/events", params={"task_id": TASK_ID}
    ).json()

    assert result["total"] == 1
    assert result["events"][0]["type"] == "local_event"
