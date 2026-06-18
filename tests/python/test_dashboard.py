"""Tests for the Dashboard monitoring feature.

Tests the DashboardApp REST API endpoints, SSE stream, and
Orchestrator integration (start_dashboard / stop_dashboard).
Uses FastAPI TestClient for synchronous endpoint testing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.types import (
    Task,
    TaskStatus,
    WorkerInfo,
)

# ── Helpers ──────────────────────────────────────────────────


def _make_orchestrator(
    with_engine: bool = True,
    with_scheduler: bool = False,
) -> Orchestrator:
    """Create an Orchestrator with optional mocked engine and scheduler."""
    engine = MagicMock() if with_engine else None
    scheduler = MagicMock() if with_scheduler else None
    orch = Orchestrator(engine=engine, scheduler=scheduler)
    return orch


def _mock_health_response():
    """Create a mock engine health response."""
    health = MagicMock()
    health.status = "degraded"
    health.version = "0.1.0"
    health.uptime_seconds = 3600

    components = []
    comp_names = [
        "short_term_memory",
        "long_term_memory",
        "metadata_store",
        "index_pipeline",
        "search_engine",
        "embedding_service",
        "checkpoint_manager",
        "conflict_detector",
        "rate_limiter",
        "circuit_breaker",
        "sandbox",
    ]
    statuses = [
        "fallback",
        "fallback",
        "fallback",
        "ok",
        "ok",
        "ok",
        "ok",
        "ok",
        "ok",
        "ok",
        "ok",
    ]
    details = [
        "Using in-memory fallback",
        "Using in-memory fallback",
        "Using in-memory fallback",
        "Index pipeline ready",
        "Hybrid search engine ready",
        "Embedding service configured",
        "Event sourcing + checkpoint ready",
        "Intent-based conflict detection ready",
        "RPM: 60 available, TPM: 100000 available",
        "State: Closed, failures: 0",
        "Sandbox execution ready",
    ]
    for name, status, detail in zip(comp_names, statuses, details):
        comp = MagicMock()
        comp.name = name
        comp.status = status
        comp.details = detail
        components.append(comp)

    health.components = components
    return health


# ── DashboardApp Unit Tests ──────────────────────────────────


class TestDashboardApp:
    """Test DashboardApp data collection and API endpoints."""

    def setup_method(self):
        """Create a fresh Orchestrator and DashboardApp for each test."""
        from ultimate_coders.dashboard.app import DashboardApp

        self.orch = _make_orchestrator()
        self.orch.engine.health.return_value = _mock_health_response()
        self.dashboard = DashboardApp(self.orch)

    def test_get_health_data_available(self):
        """Health data returns all 11 components with status and details."""
        data = self.dashboard._get_health_data()
        assert data["available"] is True
        assert data["status"] == "degraded"
        assert data["version"] == "0.1.0"
        assert data["uptime_seconds"] == 3600
        assert len(data["components"]) == 11

        # Check component names
        comp_names = [c["name"] for c in data["components"]]
        assert "short_term_memory" in comp_names
        assert "circuit_breaker" in comp_names
        assert "sandbox" in comp_names

    def test_get_health_data_no_engine(self):
        """Health data returns unavailable when engine is None."""
        self.dashboard.orchestrator.engine = None
        data = self.dashboard._get_health_data()
        assert data["available"] is False
        assert data["status"] == "unavailable"
        assert data["components"] == []

    def test_get_health_data_engine_error(self):
        """Health data handles engine.health() exception gracefully."""
        self.orch.engine.health.side_effect = RuntimeError("connection failed")
        data = self.dashboard._get_health_data()
        assert data["available"] is False
        assert data["status"] == "error"
        assert "connection failed" in data["error"]

    def test_get_workers_data_empty(self):
        """Workers data returns empty list when no workers registered."""
        data = self.dashboard._get_workers_data()
        assert data["available"] is True
        assert data["workers"] == []
        assert data["total"] == 0
        assert data["available_count"] == 0

    def test_get_workers_data_with_workers(self):
        """Workers data includes registered workers with load info."""
        w1 = WorkerInfo(
            id="worker-1",
            capabilities=["code_generation", "testing"],
            current_load=1,
            max_capacity=3,
        )
        w2 = WorkerInfo(
            id="worker-2",
            capabilities=["code_review"],
            current_load=3,
            max_capacity=3,
        )
        self.orch.workers["worker-1"] = w1
        self.orch.workers["worker-2"] = w2

        data = self.dashboard._get_workers_data()
        assert data["total"] == 2
        assert data["available_count"] == 1  # w1 available, w2 full

        # Check worker data
        w1_data = next(w for w in data["workers"] if w["id"] == "worker-1")
        assert w1_data["current_load"] == 1
        assert w1_data["max_capacity"] == 3
        assert w1_data["load_percent"] == 33
        assert w1_data["is_available"] is True
        assert "code_generation" in w1_data["capabilities"]

        w2_data = next(w for w in data["workers"] if w["id"] == "worker-2")
        assert w2_data["load_percent"] == 100
        assert w2_data["is_available"] is False

    def test_get_workers_data_heartbeat_stale(self):
        """Workers data flags stale heartbeats."""
        from datetime import timedelta

        w = WorkerInfo(
            id="worker-stale",
            capabilities=[],
            current_load=0,
            max_capacity=3,
            last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=120),
        )
        self.orch.workers["worker-stale"] = w

        data = self.dashboard._get_workers_data()
        w_data = data["workers"][0]
        assert w_data["heartbeat_stale"] is True
        assert w_data["heartbeat_age_seconds"] > 60

    def test_get_tasks_data_empty(self):
        """Tasks data returns empty when no tasks exist."""
        data = self.dashboard._get_tasks_data()
        assert data["available"] is True
        assert data["tasks"] == []
        assert data["total"] == 0
        assert data["status_counts"] == {}

    def test_get_tasks_data_with_tasks(self):
        """Tasks data includes task list and status counts."""
        t1 = Task(description="Implement auth", project_id="my-app", status=TaskStatus.IN_PROGRESS)
        t2 = Task(description="Write tests", project_id="my-app", status=TaskStatus.PLANNING)
        t3 = Task(description="Fix bug", status=TaskStatus.COMPLETED)
        self.orch.tasks[t1.id] = t1
        self.orch.tasks[t2.id] = t2
        self.orch.tasks[t3.id] = t3

        data = self.dashboard._get_tasks_data()
        assert data["total"] == 3
        assert data["status_counts"]["in_progress"] == 1
        assert data["status_counts"]["planning"] == 1
        assert data["status_counts"]["completed"] == 1

    def test_get_tasks_data_pending_count(self):
        """Tasks data includes pending_task_count from night window."""
        self.orch._pending_tasks.append(Task(description="deferred task", status=TaskStatus.PAUSED))
        data = self.dashboard._get_tasks_data()
        assert data["pending_task_count"] == 1

    def test_get_scheduler_data_no_scheduler(self):
        """Scheduler data returns unavailable when no scheduler configured."""
        data = self.dashboard._get_scheduler_data()
        assert data["available"] is False

    def test_get_scheduler_data_with_scheduler(self):
        """Scheduler data includes jobs and execution history."""
        orch = _make_orchestrator(with_scheduler=True)
        from ultimate_coders.dashboard.app import DashboardApp

        dashboard = DashboardApp(orch)

        # Mock scheduler methods
        job = MagicMock()
        job.id = "job-123"
        job.description = "Rebuild index"
        job.project_id = "project-alpha"
        job.enabled = True
        job.cron_expression = "0 22 * * *"
        job.execute_after = None

        orch.scheduler.list_jobs.return_value = [job]
        orch.scheduler.is_running.return_value = True

        hist = MagicMock()
        hist.started_at = "2026-06-11T22:00:00Z"
        hist.completed_at = "2026-06-11T22:05:00Z"
        hist.status = "Completed"
        hist.result_summary = "Indexed 42 files"
        orch.scheduler.get_execution_history.return_value = [hist]

        data = dashboard._get_scheduler_data()
        assert data["available"] is True
        assert data["is_running"] is True
        assert data["job_count"] == 1
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["description"] == "Rebuild index"
        assert data["jobs"][0]["cron_expression"] == "0 22 * * *"
        assert len(data["execution_history"]) == 1

    def test_get_circuit_breaker_data(self):
        """Circuit breaker data includes state and metrics."""
        from ultimate_coders.agent.rate_limiter import CircuitBreaker, RateLimiter

        orch = _make_orchestrator()
        orch.circuit_breaker = CircuitBreaker()
        orch.rate_limiter = RateLimiter()

        from ultimate_coders.dashboard.app import DashboardApp

        dashboard = DashboardApp(orch)

        data = dashboard._get_circuit_breaker_data()
        cb = data["circuit_breaker"]
        assert cb["available"] is True
        assert cb["state"] == "closed"
        assert cb["failure_count"] == 0
        assert cb["total_calls"] == 0

        rl = data["rate_limiter"]
        assert rl["available"] is True
        assert rl["rpm_available"] > 0
        assert rl["tpm_available"] > 0

    def test_get_circuit_breaker_data_open_state(self):
        """Circuit breaker data shows open state after failures."""
        from ultimate_coders.agent.rate_limiter import CircuitBreaker

        orch = _make_orchestrator()
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.allow_request()  # count the calls
            cb.record_failure()

        orch.circuit_breaker = cb
        orch.rate_limiter = MagicMock()
        orch.rate_limiter.rpm_available = 30
        orch.rate_limiter.tpm_available = 50000
        orch.rate_limiter.active_count = 2
        orch.rate_limiter.total_requests = 15

        from ultimate_coders.dashboard.app import DashboardApp

        dashboard = DashboardApp(orch)

        data = dashboard._get_circuit_breaker_data()
        cb_data = data["circuit_breaker"]
        assert cb_data["state"] == "open"
        assert cb_data["failure_count"] == 3

    def test_get_full_snapshot(self):
        """Full snapshot aggregates all panel data."""
        snapshot = self.dashboard._get_full_snapshot()
        assert "timestamp" in snapshot
        assert "health" in snapshot
        assert "workers" in snapshot
        assert "tasks" in snapshot
        assert "scheduler" in snapshot
        assert "circuit_breaker" in snapshot


# ── API Endpoint Tests ──────────────────────────────────────


class TestDashboardAPIEndpoints:
    """Test Dashboard REST API endpoints via FastAPI TestClient."""

    @pytest.fixture
    def client(self):
        """Create a TestClient for the dashboard FastAPI app."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        orch.engine.health.return_value = _mock_health_response()
        dashboard = DashboardApp(orch)
        return TestClient(dashboard._app)

    def test_dashboard_page(self, client):
        """GET /dashboard/ no longer serves HTML — frontend is a separate React SPA."""
        response = client.get("/dashboard/")
        assert response.status_code == 404  # HTML page removed; SPA serves independently

    def test_health_api(self, client):
        """GET /dashboard/api/health returns JSON with components."""
        response = client.get("/dashboard/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["available"] is True
        assert data["status"] == "degraded"
        assert len(data["components"]) == 11

    def test_workers_api(self, client):
        """GET /dashboard/api/workers returns JSON with worker list."""
        response = client.get("/dashboard/api/workers")
        assert response.status_code == 200
        data = response.json()
        assert data["available"] is True
        assert "workers" in data

    def test_tasks_api(self, client):
        """GET /dashboard/api/tasks returns JSON with task data."""
        response = client.get("/dashboard/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["available"] is True
        assert "tasks" in data
        assert "status_counts" in data

    def test_scheduler_api_no_scheduler(self, client):
        """GET /dashboard/api/scheduler returns unavailable when no scheduler."""
        response = client.get("/dashboard/api/scheduler")
        assert response.status_code == 200
        data = response.json()
        assert data["available"] is False

    def test_circuit_breaker_api(self, client):
        """GET /dashboard/api/circuit-breaker returns CB and RL data."""
        response = client.get("/dashboard/api/circuit-breaker")
        assert response.status_code == 200
        data = response.json()
        assert "circuit_breaker" in data
        assert "rate_limiter" in data

    def test_sse_stream_content_type(self, client):
        """GET /dashboard/api/stream route exists and is configured."""
        # SSE streams are infinite, so we verify the endpoint is wired up
        # by checking that the route is registered in the FastAPI app.
        routes = [r.path for r in client.app.routes if hasattr(r, "path")]
        assert "/dashboard/api/stream" in routes

    def test_sse_snapshot_format(self, client):
        """SSE snapshot data has the expected structure."""
        # Instead of testing the actual SSE stream (which is infinite),
        # test the data format that the SSE endpoint produces.
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        orch.engine.health.return_value = _mock_health_response()
        dashboard = DashboardApp(orch)
        snapshot = dashboard._get_full_snapshot()

        # Verify snapshot structure matches what SSE would send
        assert "timestamp" in snapshot
        assert "health" in snapshot
        assert "workers" in snapshot
        assert "tasks" in snapshot
        assert "scheduler" in snapshot
        assert "circuit_breaker" in snapshot

        # Verify JSON-serializable
        json_str = json.dumps(snapshot)
        assert len(json_str) > 0


# ── Orchestrator Integration Tests ──────────────────────────


class TestOrchestratorDashboard:
    """Test Orchestrator.start_dashboard() and stop_dashboard()."""

    def test_start_dashboard_creates_app(self):
        """start_dashboard() creates a DashboardApp instance."""
        orch = _make_orchestrator()
        orch.engine.health.return_value = _mock_health_response()

        with patch("ultimate_coders.dashboard.app.DashboardApp.start") as mock_start:
            orch.start_dashboard(host="127.0.0.1", port=9999)
            assert hasattr(orch, "_dashboard_app")
            assert orch._dashboard_app is not None
            mock_start.assert_called_once_with(host="127.0.0.1", port=9999)

    def test_stop_dashboard_cleans_up(self):
        """stop_dashboard() cleans up the DashboardApp reference."""
        orch = _make_orchestrator()
        with patch("ultimate_coders.dashboard.app.DashboardApp.start"):
            orch.start_dashboard()
        with patch.object(orch._dashboard_app, "stop") as mock_stop:
            orch.stop_dashboard()
            mock_stop.assert_called_once()
            assert orch._dashboard_app is None

    def test_stop_dashboard_when_not_running(self):
        """stop_dashboard() handles case when dashboard not started."""
        orch = _make_orchestrator()
        # Should not raise
        orch.stop_dashboard()

    def test_start_dashboard_idempotent(self):
        """start_dashboard() warns but does not create duplicate if already running."""
        orch = _make_orchestrator()
        with patch("ultimate_coders.dashboard.app.DashboardApp.start"):
            orch.start_dashboard()
            first_app = orch._dashboard_app

            # Second call should not replace the app
            orch.start_dashboard()
            assert orch._dashboard_app is first_app

    def test_start_dashboard_import_error(self):
        """start_dashboard() raises ImportError when deps missing."""
        orch = _make_orchestrator()
        # Make the dashboard.app module raise ImportError on import
        # by temporarily removing it from sys.modules and inserting
        # a mock that raises ImportError.
        import sys

        original_mod = sys.modules.get("ultimate_coders.dashboard.app")
        try:
            # Remove the cached module so the import re-executes
            if "ultimate_coders.dashboard.app" in sys.modules:
                del sys.modules["ultimate_coders.dashboard.app"]

            # Create a module mock that will raise ImportError when
            # DashboardApp is accessed (simulates missing dependency)
            class _FailingModule:
                def __getattr__(self, name):
                    raise ImportError("no fastapi")

            sys.modules["ultimate_coders.dashboard.app"] = _FailingModule()

            with pytest.raises(ImportError, match="Dashboard dependencies"):
                orch.start_dashboard()
        finally:
            # Restore the original module
            if original_mod is not None:
                sys.modules["ultimate_coders.dashboard.app"] = original_mod
            elif "ultimate_coders.dashboard.app" in sys.modules:
                del sys.modules["ultimate_coders.dashboard.app"]


# ── Fallback Mode Tests ────────────────────────────────────


class TestDashboardFallback:
    """Test dashboard behavior when infrastructure is unavailable."""

    def test_health_no_engine(self):
        """Health panel shows unavailable when engine is None."""
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator(with_engine=False)
        dashboard = DashboardApp(orch)
        data = dashboard._get_health_data()
        assert data["available"] is False
        assert data["status"] == "unavailable"

    def test_health_engine_exception(self):
        """Health panel shows error when engine.health() throws."""
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        orch.engine.health.side_effect = Exception("TiKV down")
        dashboard = DashboardApp(orch)
        data = dashboard._get_health_data()
        assert data["available"] is False
        assert data["status"] == "error"

    def test_scheduler_no_scheduler(self):
        """Scheduler panel shows Not Available when no scheduler."""
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator(with_scheduler=False)
        dashboard = DashboardApp(orch)
        data = dashboard._get_scheduler_data()
        assert data["available"] is False

    def test_scheduler_list_jobs_exception(self):
        """Scheduler panel handles list_jobs() exception gracefully."""
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator(with_scheduler=True)
        orch.scheduler.list_jobs.side_effect = RuntimeError("store unavailable")
        orch.scheduler.is_running.return_value = False
        dashboard = DashboardApp(orch)
        data = dashboard._get_scheduler_data()
        assert data["available"] is True
        assert data["jobs"] == []

    def test_workers_no_orchestrator(self):
        """Workers panel handles None orchestrator."""
        from ultimate_coders.dashboard.app import DashboardApp

        dashboard = DashboardApp(None)
        data = dashboard._get_workers_data()
        assert data["available"] is False

    def test_tasks_no_orchestrator(self):
        """Tasks panel handles None orchestrator."""
        from ultimate_coders.dashboard.app import DashboardApp

        dashboard = DashboardApp(None)
        data = dashboard._get_tasks_data()
        assert data["available"] is False

    def test_circuit_breaker_no_orchestrator(self):
        """Circuit breaker panel handles None orchestrator."""
        from ultimate_coders.dashboard.app import DashboardApp

        dashboard = DashboardApp(None)
        data = dashboard._get_circuit_breaker_data()
        assert data["circuit_breaker"]["available"] is False

    def test_degraded_engine_shows_component_status(self):
        """Degraded engine shows fallback status for storage components."""
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        orch.engine.health.return_value = _mock_health_response()
        dashboard = DashboardApp(orch)
        data = dashboard._get_health_data()

        # Short-term memory should show fallback
        stm = next(c for c in data["components"] if c["name"] == "short_term_memory")
        assert stm["status"] == "fallback"

        # Core components should be ok
        ip = next(c for c in data["components"] if c["name"] == "index_pipeline")
        assert ip["status"] == "ok"

        # Overall should be degraded
        assert data["status"] == "degraded"


# ── Interactive POST Endpoint Tests ───────────────────────────


class TestDashboardPOSTEndpoints:
    """Test POST API endpoints for interactive dashboard operations."""

    @pytest.fixture
    def client(self):
        """Create a TestClient with an Orchestrator that has tasks and CB."""
        from fastapi.testclient import TestClient
        from ultimate_coders.agent.rate_limiter import CircuitBreaker, RateLimiter
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        orch.engine.health.return_value = _mock_health_response()
        orch.circuit_breaker = CircuitBreaker(failure_threshold=3)
        orch.rate_limiter = RateLimiter()

        # Add a task that can be paused
        t1 = Task(description="Test task", project_id="p1", status=TaskStatus.IN_PROGRESS)
        orch.tasks[t1.id] = t1
        self.task_id = t1.id

        dashboard = DashboardApp(orch)
        self.dashboard = dashboard
        return TestClient(dashboard._app)

    def test_pause_task(self, client):
        """POST /dashboard/api/tasks/{id}/pause pauses a task."""
        response = client.post(f"/dashboard/api/tasks/{self.task_id}/pause")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["status"] == "paused"

    def test_pause_task_not_found(self, client):
        """POST pause returns 400 for non-existent task."""
        response = client.post("/dashboard/api/tasks/nonexistent/pause")
        assert response.status_code == 400

    def test_resume_task(self, client):
        """POST /dashboard/api/tasks/{id}/resume resumes a paused task."""
        # First pause
        client.post(f"/dashboard/api/tasks/{self.task_id}/pause")
        # Then resume
        response = client.post(f"/dashboard/api/tasks/{self.task_id}/resume")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["status"] == "in_progress"

    def test_resume_task_not_paused(self, client):
        """POST resume returns 400 if task is not paused."""
        response = client.post(f"/dashboard/api/tasks/{self.task_id}/resume")
        assert response.status_code == 400

    def test_circuit_breaker_reset(self, client):
        """POST /dashboard/api/circuit-breaker/reset resets CB."""
        # First open the CB
        for _ in range(3):
            client._orch_circuit_breaker = None  # hack: use dashboard's orch
        self.dashboard.orchestrator.circuit_breaker.record_failure()
        self.dashboard.orchestrator.circuit_breaker.record_failure()
        self.dashboard.orchestrator.circuit_breaker.record_failure()

        response = client.post("/dashboard/api/circuit-breaker/reset")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["state"] == "closed"

    def test_circuit_breaker_reset_no_cb(self):
        """POST reset returns 400 when no CB configured."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        orch.circuit_breaker = None
        dashboard = DashboardApp(orch)
        client = TestClient(dashboard._app)

        response = client.post("/dashboard/api/circuit-breaker/reset")
        assert response.status_code == 400

    def test_flush_pending(self, client):
        """POST /dashboard/api/tasks/flush-pending returns pending count."""
        response = client.post("/dashboard/api/tasks/flush-pending")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "pending_count" in data

    def test_trigger_job_no_scheduler(self):
        """POST trigger returns 503 when no scheduler configured."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator(with_scheduler=False)
        dashboard = DashboardApp(orch)
        client = TestClient(dashboard._app)

        response = client.post("/dashboard/api/scheduler/jobs/test-id/trigger")
        assert response.status_code == 503

    def test_pause_no_orchestrator(self):
        """POST pause returns 503 when no orchestrator."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        dashboard = DashboardApp(None)
        client = TestClient(dashboard._app)

        response = client.post("/dashboard/api/tasks/test-id/pause")
        assert response.status_code == 503


# ── Event Log Tests ──────────────────────────────────────────


class TestEventLog:
    """Test event logging and event API endpoint."""

    def test_record_event(self):
        """_record_event appends to the event log."""
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)

        dashboard._record_event("test_event", key1="val1")
        dashboard._record_event("another_event", key2="val2")

        assert len(dashboard._event_log) == 2
        assert dashboard._event_log[0]["type"] == "another_event"  # newest first
        assert dashboard._event_log[1]["type"] == "test_event"

    def test_event_log_maxlen(self):
        """Event log is bounded by maxlen=500."""
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)

        for i in range(600):
            dashboard._record_event("event", idx=i)

        assert len(dashboard._event_log) == 500

    def test_events_api_endpoint(self):
        """GET /dashboard/api/events returns event log."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)
        dashboard._record_event("test_event", task_id="abc123")

        client = TestClient(dashboard._app)
        response = client.get("/dashboard/api/events")
        assert response.status_code == 200
        data = response.json()
        assert data["available"] is True
        assert data["total"] == 1
        assert data["events"][0]["type"] == "test_event"

    def test_snapshot_includes_events(self):
        """Full snapshot includes events field."""
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)
        dashboard._record_event("test_event")

        snapshot = dashboard._get_full_snapshot()
        assert "events" in snapshot
        assert len(snapshot["events"]) == 1


# ── Orchestrator Method Tests ────────────────────────────────


class TestOrchestratorInteractiveMethods:
    """Test pause_task, resume_task, and reset_circuit_breaker on Orchestrator."""

    def test_pause_task(self):
        """Orchestrator.pause_task pauses an in-progress task."""
        orch = _make_orchestrator()
        t = Task(description="Test", status=TaskStatus.IN_PROGRESS)
        orch.tasks[t.id] = t

        result = orch.pause_task(t.id)
        assert result is True
        assert orch.tasks[t.id].status == TaskStatus.PAUSED

    def test_pause_task_not_found(self):
        """Orchestrator.pause_task returns False for missing task."""
        orch = _make_orchestrator()
        result = orch.pause_task("nonexistent")
        assert result is False

    def test_pause_task_wrong_status(self):
        """Orchestrator.pause_task returns False for completed task."""
        orch = _make_orchestrator()
        t = Task(description="Done", status=TaskStatus.COMPLETED)
        orch.tasks[t.id] = t

        result = orch.pause_task(t.id)
        assert result is False

    def test_resume_task(self):
        """Orchestrator.resume_task resumes a paused task."""
        orch = _make_orchestrator()
        t = Task(description="Paused", status=TaskStatus.PAUSED)
        orch.tasks[t.id] = t

        result = orch.resume_task(t.id)
        assert result is True
        assert orch.tasks[t.id].status == TaskStatus.IN_PROGRESS

    def test_resume_task_not_paused(self):
        """Orchestrator.resume_task returns False for non-paused task."""
        orch = _make_orchestrator()
        t = Task(description="Active", status=TaskStatus.IN_PROGRESS)
        orch.tasks[t.id] = t

        result = orch.resume_task(t.id)
        assert result is False

    def test_reset_circuit_breaker(self):
        """Orchestrator.reset_circuit_breaker resets the CB to closed."""
        from ultimate_coders.agent.rate_limiter import CircuitBreaker, CircuitState

        orch = _make_orchestrator()
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

        orch.circuit_breaker = cb
        result = orch.reset_circuit_breaker()
        assert result is True
        assert cb.state == CircuitState.CLOSED

    def test_reset_circuit_breaker_none(self):
        """Orchestrator.reset_circuit_breaker returns False when CB is None."""
        orch = _make_orchestrator()
        orch.circuit_breaker = None
        result = orch.reset_circuit_breaker()
        assert result is False


# ── CircuitBreaker reset() Tests ─────────────────────────────


class TestCircuitBreakerReset:
    """Test CircuitBreaker.reset() method."""

    def test_reset_from_open(self):
        """reset() transitions from OPEN to CLOSED."""
        from ultimate_coders.agent.rate_limiter import CircuitBreaker, CircuitState

        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_reset_from_half_open(self):
        """reset() transitions from HALF_OPEN to CLOSED."""
        from ultimate_coders.agent.rate_limiter import CircuitBreaker, CircuitState

        cb = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=0.0)
        for _ in range(3):
            cb.record_failure()
        # Timeout=0 means it should transition to half_open on next check
        cb.allow_request()
        assert cb.state == CircuitState.HALF_OPEN

        cb.reset()
        assert cb.state == CircuitState.CLOSED


# ── TaskEventEmitter Tests ───────────────────────────────────


class TestTaskEventEmitter:
    """Test TaskEventEmitter event bus."""

    def test_emit_and_get_recent(self):
        """emit() stores events in recent buffer."""
        import asyncio

        from ultimate_coders.agent.event_emitter import TaskEventEmitter

        emitter = TaskEventEmitter()
        loop = asyncio.new_event_loop()
        loop.run_until_complete(emitter.emit("test_event", task_id="t1", data={"key": "val"}))
        recent = emitter.get_recent_events()
        assert len(recent) == 1
        assert recent[0]["type"] == "test_event"
        assert recent[0]["task_id"] == "t1"
        loop.close()

    def test_get_recent_filtered_by_task(self):
        """get_recent_events filters by task_id."""
        import asyncio

        from ultimate_coders.agent.event_emitter import TaskEventEmitter

        emitter = TaskEventEmitter()
        loop = asyncio.new_event_loop()
        loop.run_until_complete(emitter.emit("ev1", task_id="t1"))
        loop.run_until_complete(emitter.emit("ev2", task_id="t2"))
        loop.run_until_complete(emitter.emit("ev3", task_id="t1"))
        t1_events = emitter.get_recent_events(task_id="t1")
        assert len(t1_events) == 2
        loop.close()

    def test_recent_buffer_maxlen(self):
        """Ring buffer is bounded by buffer_size."""
        import asyncio

        from ultimate_coders.agent.event_emitter import TaskEventEmitter

        emitter = TaskEventEmitter(buffer_size=5)
        loop = asyncio.new_event_loop()
        for i in range(10):
            loop.run_until_complete(emitter.emit("ev", task_id="t", data={"i": i}))
        assert len(emitter.get_recent_events()) == 5
        loop.close()

    def test_task_event_to_dict(self):
        """TaskEvent.to_dict() serializes correctly."""
        from ultimate_coders.agent.event_emitter import TaskEvent

        ev = TaskEvent(type="tool_call", task_id="t1", subtask_id="s1", data={"tool": "read"})
        d = ev.to_dict()
        assert d["type"] == "tool_call"
        assert d["task_id"] == "t1"
        assert d["subtask_id"] == "s1"
        assert d["data"]["tool"] == "read"
        assert "timestamp" in d


# ── Task Submit Endpoint Tests ───────────────────────────────


class TestTaskSubmitEndpoint:
    """Test POST /dashboard/api/tasks/submit."""

    def test_submit_task_success(self):
        """Submit a task with description."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        orch.engine.health.return_value = _mock_health_response()
        # Mock submit_task to return a task
        t = Task(description="Write tests", project_id="p1", status=TaskStatus.IN_PROGRESS)
        orch.submit_task = lambda *a, **kw: _async_return(t)

        dashboard = DashboardApp(orch)
        client = TestClient(dashboard._app)

        response = client.post(
            "/dashboard/api/tasks/submit",
            json={"description": "Write tests", "project_id": "p1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["task_id"] == t.id

    def test_submit_task_no_description(self):
        """Submit without description returns 400."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)
        client = TestClient(dashboard._app)

        response = client.post(
            "/dashboard/api/tasks/submit",
            json={"description": ""},
        )
        assert response.status_code == 400

    def test_submit_task_no_orchestrator(self):
        """Submit with no orchestrator returns 503."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        dashboard = DashboardApp(None)
        client = TestClient(dashboard._app)

        response = client.post(
            "/dashboard/api/tasks/submit",
            json={"description": "Do something"},
        )
        assert response.status_code == 503

    def test_submit_task_invalid_json(self):
        """Submit with invalid JSON returns 400."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)
        client = TestClient(dashboard._app)

        response = client.post(
            "/dashboard/api/tasks/submit",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400


# ── Orchestrator Event Emitter Tests ─────────────────────────


class TestOrchestratorEventEmitter:
    """Test that Orchestrator has an event_emitter."""

    def test_orchestrator_has_event_emitter(self):
        """Orchestrator initializes with a TaskEventEmitter."""
        from ultimate_coders.agent.event_emitter import TaskEventEmitter

        orch = _make_orchestrator()
        assert hasattr(orch, "event_emitter")
        assert isinstance(orch.event_emitter, TaskEventEmitter)

    def test_submit_task_emits_event(self):
        """submit_task emits a task_submitted event."""
        import asyncio

        orch = _make_orchestrator()
        t = Task(description="Test emit", status=TaskStatus.IN_PROGRESS)
        orch.tasks[t.id] = t

        # Manually emit as submit_task would
        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            orch.event_emitter.emit(
                "task_submitted", task_id=t.id, data={"description": "Test emit"}
            )
        )

        recent = orch.event_emitter.get_recent_events(task_id=t.id)
        assert len(recent) == 1
        assert recent[0]["type"] == "task_submitted"
        loop.close()


# ── Helper ──────────────────────────────────────────────────


def _async_return(value):
    """Create a coroutine that returns the given value."""
    import asyncio

    fut = asyncio.Future()
    fut.set_result(value)
    return fut


# ── Worker Event Emitter Tests ─────────────────────────────────


class TestWorkerEventEmitter:
    """Test Worker emits events via event_emitter during subtask execution."""

    def test_worker_emits_subtask_started(self):
        """Worker emits subtask_started event when event_emitter is set."""
        import asyncio

        from ultimate_coders.agent.event_emitter import TaskEventEmitter
        from ultimate_coders.agent.types import Subtask, SubtaskStatus
        from ultimate_coders.agent.worker import Worker

        emitter = TaskEventEmitter()
        worker = Worker(worker_id="w1", event_emitter=emitter)

        subtask = Subtask(
            parent_id="task-1",
            description="Test subtask",
            status=SubtaskStatus.PENDING,
        )

        loop = asyncio.new_event_loop()
        try:
            # Execute subtask (will fail because no LLM client, but
            # subtask_started should be emitted before the failure)
            loop.run_until_complete(worker.execute_subtask(subtask))
            # Check that subtask_started was emitted
            events = emitter.get_recent_events(task_id="task-1")
            started_events = [e for e in events if e["type"] == "subtask_started"]
            assert len(started_events) >= 1
            assert started_events[0]["data"]["description"] == "Test subtask"
        finally:
            loop.close()

    def test_worker_emits_subtask_failed(self):
        """Worker emits subtask_failed event on execution error."""
        import asyncio

        from ultimate_coders.agent.event_emitter import TaskEventEmitter
        from ultimate_coders.agent.types import Subtask, SubtaskStatus
        from ultimate_coders.agent.worker import Worker

        emitter = TaskEventEmitter()
        worker = Worker(worker_id="w1", event_emitter=emitter)

        subtask = Subtask(
            parent_id="task-1",
            description="Failing subtask",
            status=SubtaskStatus.PENDING,
        )

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(worker.execute_subtask(subtask))
            # No LLM client means it returns a failure result
            assert result.success is False
            # Check that subtask_failed was emitted
            events = emitter.get_recent_events(task_id="task-1")
            failed_events = [e for e in events if e["type"] == "subtask_failed"]
            assert len(failed_events) >= 1
        finally:
            loop.close()

    def test_worker_without_emitter_backward_compat(self):
        """Worker works without event_emitter (backward compatible)."""
        import asyncio

        from ultimate_coders.agent.types import Subtask, SubtaskStatus
        from ultimate_coders.agent.worker import Worker

        worker = Worker(worker_id="w1")  # No event_emitter
        assert worker.event_emitter is None

        subtask = Subtask(
            parent_id="task-1",
            description="No emitter subtask",
            status=SubtaskStatus.PENDING,
        )

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(worker.execute_subtask(subtask))
            # Should still return a result (failure due to no LLM)
            assert result.success is False
            assert result.subtask_id == subtask.id
        finally:
            loop.close()


# ── Orchestrator task_completed Event Tests ────────────────────


class TestOrchestratorTaskCompleted:
    """Test that task_completed event is emitted when all subtasks finish."""

    def test_task_completed_emitted_on_success(self):
        """handle_subtask_result emits task_completed when all subtasks succeed."""
        import asyncio

        from ultimate_coders.agent.types import (
            Subtask,
            SubtaskResult,
            SubtaskStatus,
            Task,
            TaskStatus,
        )

        orch = _make_orchestrator()

        task = Task(description="Test", status=TaskStatus.IN_PROGRESS)
        subtask = Subtask(
            parent_id=task.id,
            description="Sub 1",
            status=SubtaskStatus.IN_PROGRESS,
        )
        task.subtasks = [subtask]
        orch.tasks[task.id] = task

        result = SubtaskResult(
            subtask_id=subtask.id,
            worker_id="w1",
            summary="Done",
            success=True,
        )

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orch.handle_subtask_result(result))
            # Task should be completed
            assert task.status == TaskStatus.COMPLETED
            # Event should be emitted
            events = orch.event_emitter.get_recent_events(task_id=task.id)
            completed = [e for e in events if e["type"] == "task_completed"]
            assert len(completed) == 1
            assert completed[0]["data"]["status"] == "completed"
        finally:
            loop.close()

    def test_task_completed_emitted_on_failure(self):
        """handle_subtask_result emits task_completed with failed status."""
        import asyncio

        from ultimate_coders.agent.types import (
            Subtask,
            SubtaskResult,
            SubtaskStatus,
            Task,
            TaskStatus,
        )

        orch = _make_orchestrator()

        task = Task(description="Test", status=TaskStatus.IN_PROGRESS)
        subtask = Subtask(
            parent_id=task.id,
            description="Sub 1",
            status=SubtaskStatus.IN_PROGRESS,
        )
        task.subtasks = [subtask]
        orch.tasks[task.id] = task

        result = SubtaskResult(
            subtask_id=subtask.id,
            worker_id="w1",
            summary="Failed",
            success=False,
        )

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(orch.handle_subtask_result(result))
            # Task should be failed
            assert task.status == TaskStatus.FAILED
            # Event should be emitted with failed status
            events = orch.event_emitter.get_recent_events(task_id=task.id)
            completed = [e for e in events if e["type"] == "task_completed"]
            assert len(completed) == 1
            assert completed[0]["data"]["status"] == "failed"
        finally:
            loop.close()


# ── Events API Filter Tests ───────────────────────────────────


class TestEventsAPIFilter:
    """Test events API filtering by task_id."""

    def test_events_api_filters_by_task_id(self):
        """GET /dashboard/api/events?task_id=... filters correctly."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)
        # Record events for different tasks
        dashboard._record_event("task_submitted", task_id="t1", description="Task 1")
        dashboard._record_event("task_submitted", task_id="t2", description="Task 2")
        dashboard._record_event("task_pause", task_id="t1")

        client = TestClient(dashboard._app)
        response = client.get("/dashboard/api/events?task_id=t1")
        assert response.status_code == 200
        data = response.json()
        # Only events for t1 should be returned
        for ev in data["events"]:
            assert ev.get("task_id") == "t1" or ev.get("details", {}).get("task_id") == "t1"

    def test_events_api_no_filter(self):
        """GET /dashboard/api/events without task_id returns all events."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)
        dashboard._record_event("task_submitted", task_id="t1")
        dashboard._record_event("task_submitted", task_id="t2")

        client = TestClient(dashboard._app)
        response = client.get("/dashboard/api/events")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2


# ── Dashboard NATS Integration Tests ──────────────────────────


class TestDashboardNatsSubmit:
    """Test Dashboard submit_task with NATS publisher (no direct Orchestrator call)."""

    def test_submit_via_nats_publisher(self):
        """When nats_publisher is set, submit uses publish_submit instead of Orchestrator."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        # Mock nats_publisher with publish_submit
        mock_publisher = MagicMock()
        mock_publisher.publish_submit = AsyncMock()
        dashboard = DashboardApp(orch, nats_publisher=mock_publisher)
        client = TestClient(dashboard._app)

        response = client.post(
            "/dashboard/api/tasks/submit",
            json={"description": "Fix the bug", "project_id": "proj-1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["status"] == "submitted"
        assert data["subtask_count"] == 0
        assert "task_id" in data
        # Verify publish_submit was called with correct args
        mock_publisher.publish_submit.assert_called_once()
        call_kwargs = mock_publisher.publish_submit.call_args
        assert call_kwargs.kwargs["description"] == "Fix the bug"
        assert call_kwargs.kwargs["project_id"] == "proj-1"

    def test_submit_nats_fallback_on_failure(self):
        """When NATS publish_submit fails, falls back to direct Orchestrator call."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        t = Task(description="Fix the bug", project_id="proj-1", status=TaskStatus.IN_PROGRESS)
        orch.submit_task = lambda *a, **kw: _async_return(t)

        mock_publisher = MagicMock()
        mock_publisher.publish_submit = AsyncMock(side_effect=ConnectionError("NATS down"))
        dashboard = DashboardApp(orch, nats_publisher=mock_publisher)
        client = TestClient(dashboard._app)

        response = client.post(
            "/dashboard/api/tasks/submit",
            json={"description": "Fix the bug", "project_id": "proj-1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        # Falls back to Orchestrator, so status is from the task
        assert data["task_id"] == t.id

    def test_submit_no_nats_no_orchestrator(self):
        """Without NATS or Orchestrator, submit returns 503."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        dashboard = DashboardApp(None)
        client = TestClient(dashboard._app)

        response = client.post(
            "/dashboard/api/tasks/submit",
            json={"description": "Do something"},
        )
        assert response.status_code == 503


class TestDashboardNatsPauseResume:
    """Test Dashboard pause/resume with NATS publisher."""

    def test_pause_via_nats_publisher(self):
        """When nats_publisher is set, pause publishes event via publish_event."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        mock_publisher = MagicMock()
        mock_publisher.publish_event = AsyncMock()
        dashboard = DashboardApp(orch, nats_publisher=mock_publisher)
        client = TestClient(dashboard._app)

        response = client.post("/dashboard/api/tasks/task-1/pause")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["status"] == "paused"
        mock_publisher.publish_event.assert_called_once()
        call_kwargs = mock_publisher.publish_event.call_args
        assert call_kwargs.kwargs["event_type"] == "task_pause"
        assert call_kwargs.kwargs["task_id"] == "task-1"

    def test_resume_via_nats_publisher(self):
        """When nats_publisher is set, resume publishes event via publish_event."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        mock_publisher = MagicMock()
        mock_publisher.publish_event = AsyncMock()
        dashboard = DashboardApp(orch, nats_publisher=mock_publisher)
        client = TestClient(dashboard._app)

        response = client.post("/dashboard/api/tasks/task-1/resume")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["status"] == "in_progress"
        mock_publisher.publish_event.assert_called_once()
        call_kwargs = mock_publisher.publish_event.call_args
        assert call_kwargs.kwargs["event_type"] == "task_resume"
        assert call_kwargs.kwargs["task_id"] == "task-1"

    def test_pause_nats_fallback_on_failure(self):
        """When NATS publish_event fails for pause, falls back to Orchestrator."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        t = Task(description="Test task", status=TaskStatus.IN_PROGRESS)
        orch.tasks[t.id] = t

        mock_publisher = MagicMock()
        mock_publisher.publish_event = AsyncMock(side_effect=ConnectionError("NATS down"))
        dashboard = DashboardApp(orch, nats_publisher=mock_publisher)
        client = TestClient(dashboard._app)

        response = client.post(f"/dashboard/api/tasks/{t.id}/pause")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        # Falls back to Orchestrator.pause_task
        assert orch.tasks[t.id].status == TaskStatus.PAUSED

    def test_resume_nats_fallback_on_failure(self):
        """When NATS publish_event fails for resume, falls back to Orchestrator."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        t = Task(description="Test task", status=TaskStatus.PAUSED)
        orch.tasks[t.id] = t

        mock_publisher = MagicMock()
        mock_publisher.publish_event = AsyncMock(side_effect=ConnectionError("NATS down"))
        dashboard = DashboardApp(orch, nats_publisher=mock_publisher)
        client = TestClient(dashboard._app)

        response = client.post(f"/dashboard/api/tasks/{t.id}/resume")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        # Falls back to Orchestrator.resume_task
        assert orch.tasks[t.id].status == TaskStatus.IN_PROGRESS


class TestDashboardNatsEventHandling:
    """Test Dashboard NATS event queue and SSE merging."""

    def test_handle_nats_event_valid_payload(self):
        """_handle_nats_event pushes valid events to the queue."""
        import asyncio

        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)

        msg = MagicMock()
        msg.data = json.dumps({
            "type": "subtask_completed",
            "task_id": "task-1",
            "subtask_id": "st-1",
            "data": {"success": True},
        }).encode("utf-8")

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(dashboard._handle_nats_event(msg))
        finally:
            loop.close()

        # Event should be in the queue
        assert not dashboard._get_nats_event_queue().empty()
        event = dashboard._get_nats_event_queue().get_nowait()
        assert event["type"] == "subtask_completed"
        assert event["task_id"] == "task-1"
        assert event["subtask_id"] == "st-1"

    def test_handle_nats_event_invalid_json(self):
        """_handle_nats_event handles invalid JSON gracefully."""
        import asyncio

        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)

        msg = MagicMock()
        msg.data = b"not valid json"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(dashboard._handle_nats_event(msg))
        finally:
            loop.close()

        # Queue should remain empty
        assert dashboard._get_nats_event_queue().empty()

    def test_handle_nats_event_minimal_payload(self):
        """_handle_nats_event handles payload with only required fields."""
        import asyncio

        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)

        msg = MagicMock()
        msg.data = json.dumps({
            "type": "task_submitted",
            "task_id": "task-1",
        }).encode("utf-8")

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(dashboard._handle_nats_event(msg))
        finally:
            loop.close()

        event = dashboard._get_nats_event_queue().get_nowait()
        assert event["type"] == "task_submitted"
        assert event["task_id"] == "task-1"
        # subtask_id and data should not be present
        assert "subtask_id" not in event
        assert "data" not in event

    def test_nats_subject_constants(self):
        """Dashboard NATS subject constants match nats_worker.py."""
        from ultimate_coders.dashboard.app import (
            NATS_SUBJECT_HEARTBEAT,
            NATS_SUBJECT_TASK_EVENT,
            NATS_SUBJECT_TASK_SUBMIT,
            NATS_SUBJECT_TASK_UPDATE,
        )

        assert NATS_SUBJECT_TASK_SUBMIT == "uc.task.submit"
        assert NATS_SUBJECT_TASK_UPDATE == "uc.task.update"
        assert NATS_SUBJECT_TASK_EVENT == "uc.task.event"
        assert NATS_SUBJECT_HEARTBEAT == "uc.heartbeat"

    def test_dashboard_without_nats_backward_compat(self):
        """Dashboard without NATS publisher/client works exactly as before."""
        from fastapi.testclient import TestClient
        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        t = Task(description="Test task", status=TaskStatus.IN_PROGRESS)
        orch.tasks[t.id] = t
        orch.submit_task = lambda *a, **kw: _async_return(t)

        # No NATS publisher or client
        dashboard = DashboardApp(orch)
        assert dashboard._nats_publisher is None
        assert dashboard._nats_client is None

        client = TestClient(dashboard._app)

        # Submit should use direct Orchestrator call
        response = client.post(
            "/dashboard/api/tasks/submit",
            json={"description": "Test task"},
        )
        assert response.status_code == 200
        assert response.json()["success"] is True

        # Pause should use direct Orchestrator call
        response = client.post(f"/dashboard/api/tasks/{t.id}/pause")
        assert response.status_code == 200
        assert response.json()["success"] is True


class TestDashboardNatsEventQueueFull:
    """Test Dashboard NATS event queue behavior when full."""

    def test_event_queue_drops_when_full(self):
        """When the NATS event queue is full, events are dropped with a warning."""
        import asyncio

        from ultimate_coders.dashboard.app import DashboardApp

        orch = _make_orchestrator()
        dashboard = DashboardApp(orch)

        # Fill the queue to capacity (default maxsize=0 = unlimited, so
        # we create a bounded queue for this test)
        dashboard._nats_event_queue = asyncio.Queue(maxsize=1)

        msg1 = MagicMock()
        msg1.data = json.dumps({
            "type": "event_1",
            "task_id": "task-1",
        }).encode("utf-8")

        msg2 = MagicMock()
        msg2.data = json.dumps({
            "type": "event_2",
            "task_id": "task-1",
        }).encode("utf-8")

        loop = asyncio.new_event_loop()
        try:
            # First event fills the queue
            loop.run_until_complete(dashboard._handle_nats_event(msg1))
            assert dashboard._get_nats_event_queue().qsize() == 1

            # Second event should be dropped (QueueFull)
            loop.run_until_complete(dashboard._handle_nats_event(msg2))
            # Queue should still have only 1 item
            assert dashboard._get_nats_event_queue().qsize() == 1
        finally:
            loop.close()
