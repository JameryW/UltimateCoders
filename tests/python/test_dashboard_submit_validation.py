"""Invalid HTTP task input must be rejected before any task is published."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from ultimate_coders.dashboard.app import DashboardApp


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        1,
        True,
        {"description": 12},
        {"description": []},
        {"description": "work", "project_id": []},
    ],
)
def test_invalid_submit_fields_return_bad_request_without_publishing(body):
    with patch("ultimate_coders.dashboard.app.MetricsAggregator"):
        dashboard = DashboardApp(None)
    publisher = AsyncMock()
    dashboard._nats_publisher = publisher
    with TestClient(dashboard._app, raise_server_exceptions=False) as client:
        response = client.post(
            "/dashboard/api/tasks/submit",
            content=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json()["success"] is False
    publisher.publish_submit.assert_not_called()
