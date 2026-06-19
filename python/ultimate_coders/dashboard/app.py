"""DashboardApp — FastAPI application for the monitoring dashboard.

Provides REST API endpoints and SSE stream for real-time cluster
monitoring. Embedded in the Orchestrator process for zero-latency
access to in-memory state.

The frontend is a separate React SPA (dashboard/ directory) that
connects to these API endpoints. Jinja2 templates and static file
serving have been removed — the SPA is built and deployed independently.

NATS integration (optional):
    When a NATS client is provided, the Dashboard:
    - Publishes task submit/pause/resume to NATS subjects so the
      gRPC TaskStore stays in sync with TUI consumers.
    - Subscribes to ``uc.task.event`` and merges those events into
      the SSE stream, giving visibility into tasks submitted via
      gRPC/TUI that are processed by the independent NATS consumer.
    When NATS is unavailable, the Dashboard falls back to direct
    Orchestrator calls (legacy mode).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _status_str(obj: Any) -> str:
    """Extract status string from an object with .status attribute."""
    s = obj.status
    return s.value if hasattr(s, "value") else str(s)


# ── NATS subject constants (must match nats_worker.py + Rust server.rs) ──

NATS_SUBJECT_TASK_SUBMIT: str = "uc.task.submit"
NATS_SUBJECT_TASK_UPDATE: str = "uc.task.update"
NATS_SUBJECT_TASK_EVENT: str = "uc.task.event"
NATS_SUBJECT_HEARTBEAT: str = "uc.heartbeat"


class DashboardApp:
    """FastAPI-based monitoring dashboard embedded in the Orchestrator.

    Reads Orchestrator state directly from memory (workers, tasks,
    scheduler) and engine health via PyO3/Rust. Pushes updates to
    the browser via SSE every 5 seconds.

    Auth gate:
        When the ``DASHBOARD_PASSWORD`` environment variable is set,
        non-localhost requests must include the password as a
        ``Bearer`` token (Authorization header) or ``?token=`` query
        parameter.  Localhost (127.0.0.1, ::1) bypasses auth.
        If the variable is unset, auth is disabled entirely.

    Usage:
        app = DashboardApp(orchestrator)
        app.start(host="0.0.0.0", port=8080)
        # ...
        app.stop()
    """

    def __init__(
        self,
        orchestrator: Any,
        nats_publisher: Any = None,
        nats_client: Any = None,
    ) -> None:
        """Create the dashboard app.

        Args:
            orchestrator: The Orchestrator instance to monitor.
            nats_publisher: Optional NatsPublisher instance. When set,
                task submit/pause/resume are routed through NATS so that
                the gRPC TaskStore stays in sync with TUI consumers.
                When None, falls back to direct Orchestrator calls (legacy mode).
            nats_client: Optional nats-py Client instance. When set,
                the Dashboard subscribes to ``uc.task.event`` and merges
                those events into the SSE stream. This gives the Dashboard
                visibility into tasks submitted via gRPC/TUI. When None,
                only local TaskEventEmitter events are streamed.
        """
        self.orchestrator = orchestrator
        self._nats_publisher = nats_publisher
        self._nats_client = nats_client
        # ponytail: lazy Queue — Python 3.9 asyncio.Queue() needs a running
        # event loop at init time, which doesn't exist in synchronous tests.
        self._nats_event_queue: asyncio.Queue[dict[str, Any] | None] | None = None
        self._nats_subscriptions: list[Any] = []
        self._app = FastAPI(title="UltimateCoders Dashboard")
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._event_log: deque[dict[str, Any]] = deque(maxlen=500)
        # Auth configuration
        self._dashboard_password: str | None = os.environ.get("DASHBOARD_PASSWORD") or None
        # Connect to Orchestrator's event emitter if available
        self.event_emitter = getattr(orchestrator, "event_emitter", None) if orchestrator else None
        self._setup_routes()

    # ── Auth ─────────────────────────────────────────────────────────

    def _check_auth(self, request: Request) -> JSONResponse | None:
        """Return a 401 JSON response if auth is required and fails.

        Auth logic:
        - If ``DASHBOARD_PASSWORD`` is not set, auth is disabled (returns None).
        - Localhost requests (127.0.0.1, ::1) bypass auth.
        - Non-localhost must supply the password via ``Authorization: Bearer <pwd>``
          header or ``?token=<pwd>`` query parameter.
        - Returns None on success, or a 401 JSONResponse on failure.
        """
        if not self._dashboard_password:
            return None

        # Localhost bypass
        client_host = request.client.host if request.client else ""
        if client_host in ("127.0.0.1", "::1", "localhost"):
            return None

        # Check Bearer token
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if token == self._dashboard_password:
                return None

        # Check query parameter
        token_param = request.query_params.get("token")
        if token_param == self._dashboard_password:
            return None

        # Auth failed
        return JSONResponse(
            {
                "error": "Unauthorized",
                "detail": "Valid Bearer token or token query parameter required",
            },
            status_code=401,
        )

    def _setup_routes(self) -> None:
        """Configure FastAPI routes and middleware."""
        app = self._app

        # CORS middleware — when DASHBOARD_PASSWORD is set, restrict origins
        # to UC_CORS_ORIGINS env var (comma-separated); otherwise allow all.
        cors_origins: list[str] = ["*"]
        if self._dashboard_password:
            env_origins = os.environ.get("UC_CORS_ORIGINS", "")
            if env_origins:
                cors_origins = [o.strip() for o in env_origins.split(",") if o.strip()]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

        # Mount static files — REMOVED: frontend is now a separate React SPA
        # Static files and Jinja2 templates are no longer served by the backend

        # API endpoints

        @app.get("/dashboard/api/health")
        async def health_api(request: Request):
            """Return engine health JSON."""
            if (resp := self._check_auth(request)) is not None:
                return resp
            return JSONResponse(self._get_health_data())

        @app.get("/dashboard/api/workers")
        async def workers_api(request: Request):
            """Return worker list JSON."""
            if (resp := self._check_auth(request)) is not None:
                return resp
            return JSONResponse(self._get_workers_data())

        @app.get("/dashboard/api/tasks")
        async def tasks_api(request: Request):
            """Return task status JSON."""
            if (resp := self._check_auth(request)) is not None:
                return resp
            return JSONResponse(self._get_tasks_data())

        @app.get("/dashboard/api/scheduler")
        async def scheduler_api(request: Request):
            """Return scheduler status JSON."""
            if (resp := self._check_auth(request)) is not None:
                return resp
            return JSONResponse(self._get_scheduler_data())

        @app.get("/dashboard/api/circuit-breaker")
        async def circuit_breaker_api(request: Request):
            """Return circuit breaker and rate limiter JSON."""
            if (resp := self._check_auth(request)) is not None:
                return resp
            return JSONResponse(self._get_circuit_breaker_data())

        @app.get("/dashboard/api/stream")
        async def stream(request: Request):
            """SSE endpoint: push real-time events + periodic full snapshots.

            Uses a hybrid approach:
            - Immediate task events from both the local TaskEventEmitter
              AND NATS ``uc.task.event`` (if connected) via 'task_event' SSE type
            - Full state snapshot every 5 seconds via 'update' SSE type

            When NATS is available, events from the independent NATS consumer
            (e.g., tasks submitted via gRPC/TUI) are merged into the stream.

            Auth: respects the same DASHBOARD_PASSWORD gate as other endpoints.
            Token can be passed as ``?token=<pwd>`` query parameter.
            """
            # Auth check for SSE — token must come via query param (no headers in EventSource)
            if (resp := self._check_auth(request)) is not None:
                return resp

            from sse_starlette.sse import EventSourceResponse

            # ponytail: monotonic event id for SSE resume + heartbeat
            event_id = 0
            loop = asyncio.get_running_loop()
            last_heartbeat = loop.time()
            last_snapshot = loop.time()

            async def cancellable_sleep(seconds: float) -> None:
                """Sleep that checks client disconnect every 0.5s."""
                elapsed = 0.0
                step = 0.5
                while elapsed < seconds:
                    if await request.is_disconnected():
                        return
                    await asyncio.sleep(min(step, seconds - elapsed))
                    elapsed += step

            async def event_generator():
                nonlocal event_id, last_heartbeat, last_snapshot

                while True:
                    if await request.is_disconnected():
                        break

                    # Heartbeat comment every 15s to prevent browser timeout
                    now = loop.time()
                    if now - last_heartbeat >= 15:
                        last_heartbeat = now
                        yield {"comment": "heartbeat"}

                    # Check both event sources fairly: first drain NATS queue
                    # (non-blocking), then wait on local emitter. This prevents
                    # a busy local emitter from starving NATS events.
                    had_event = False
                    if self._nats_client is not None:
                        try:
                            while True:
                                nats_event = self._get_nats_event_queue().get_nowait()
                                if nats_event is not None:
                                    self._event_log.appendleft(nats_event)
                                    event_id += 1
                                    yield {
                                        "id": str(event_id),
                                        "event": "task_event",
                                        "data": json.dumps(nats_event),
                                    }
                                    had_event = True  # noqa: F841 — used for side-effect tracking
                                else:
                                    break
                        except asyncio.QueueEmpty:
                            pass

                    # Then try local emitter (with short timeout)
                    if self.event_emitter is not None:
                        event = await self.event_emitter.wait_for_event(timeout=1.0)
                        if event is not None:
                            self._event_log.appendleft(event.to_dict())
                            event_id += 1
                            yield {
                                "id": str(event_id),
                                "event": "task_event",
                                "data": json.dumps(event.to_dict()),
                            }
                            continue  # Check for more events immediately

                    # Periodic full snapshot — interval adapts to activity.
                    # Active (events this cycle): 3s. Idle: 10s.
                    snapshot_interval = 3.0 if had_event else 10.0
                    now = loop.time()
                    if now - last_snapshot >= snapshot_interval:
                        snapshot = self._get_full_snapshot()
                        last_snapshot = now
                        event_id += 1
                        yield {
                            "id": str(event_id),
                            "event": "update",
                            "data": json.dumps(snapshot),
                        }
                    # ponytail: short sleep — detects disconnect within 0.5s
                    await cancellable_sleep(1)

            return EventSourceResponse(event_generator())

        # ── Task Submit Endpoint ─────────────────────────────────

        @app.post("/dashboard/api/tasks/submit")
        async def submit_task_api(request: Request):
            """Submit a new code development task.

            When NATS is configured, publishes the task to ``uc.task.submit``
            so it goes through the gRPC TaskStore → NATS → Python Orchestrator
            pipeline (same path as TUI).  Otherwise falls back to direct
            Orchestrator call (legacy mode, no TaskStore sync).

            Accepts a JSON body with:
            - description (required): Task description.
            - project_id (optional): Project/repository context.
            """
            if (resp := self._check_auth(request)) is not None:
                return resp
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    {"success": False, "error": "Invalid JSON body"},
                    status_code=400,
                )

            description = body.get("description", "").strip()
            if not description:
                return JSONResponse(
                    {"success": False, "error": "description is required"},
                    status_code=400,
                )

            project_id = body.get("project_id", "")

            # ── NATS path ───────────────────────────────────────────
            if self._nats_publisher is not None:
                task_id = str(uuid4())
                try:
                    await self._nats_publisher.publish_submit(
                        task_id=task_id,
                        description=description,
                        project_id=project_id,
                    )
                    # #3: Wait for nats_worker to process with retry — 0.5s × 2 attempts
                    task = None
                    for _ in range(2):
                        await asyncio.sleep(0.5)
                        orch = self.orchestrator
                        if orch is not None and task_id in orch.tasks:
                            task = orch.tasks[task_id]
                            break
                    if task is not None:
                        return JSONResponse({
                            "success": True,
                            "task_id": task.id,
                            "status": _status_str(task),
                            "subtask_count": len(task.subtasks),
                            "subtasks": [
                                {
                                    "id": st.id,
                                    "description": st.description,
                                    "status": _status_str(st),
                                    "depends_on": st.depends_on,
                                }
                                for st in task.subtasks
                            ],
                        })
                    # Task not yet in Orchestrator state — return with pending flag.
                    # Status "submitted" matches the SSE task_submitted event type.
                    return JSONResponse({
                        "success": True,
                        "task_id": task_id,
                        "status": "submitted",
                        "subtask_count": 0,
                        "subtasks": [],
                        "pending": True,
                    })
                except Exception as e:
                    logger.warning("NATS publish failed: %s, falling back", e)

            # ── Legacy direct-Orchestrator path ──────────────────────
            orch = self.orchestrator
            if orch is None:
                return JSONResponse(
                    {"success": False, "error": "Orchestrator not available"},
                    status_code=503,
                )

            try:
                task = await orch.submit_task(description, project_id=project_id)
            except Exception as e:
                logger.error("Failed to submit task: %s", e, exc_info=True)
                return JSONResponse(
                    {"success": False, "error": str(e)},
                    status_code=500,
                )

            self._record_event("task_submitted", task_id=task.id, description=description)

            return JSONResponse(
                {
                    "success": True,
                    "task_id": task.id,
                    "status": task.status.value,
                    "subtask_count": len(task.subtasks),
                    "subtasks": [
                        {
                            "id": st.id,
                            "description": st.description,
                            "status": st.status.value,
                            "depends_on": st.depends_on,
                        }
                        for st in task.subtasks
                    ],
                }
            )

        # ── POST Endpoints (Interactive Operations) ────────────

        @app.post("/dashboard/api/tasks/{task_id}/pause")
        async def pause_task_api(task_id: str, request: Request):
            """Pause a running task."""
            if (resp := self._check_auth(request)) is not None:
                return resp
            # NATS path: publish control event for gRPC server / nats_worker
            if self._nats_publisher is not None:
                try:
                    await self._nats_publisher.publish_event(
                        event_type="task_pause",
                        task_id=task_id,
                    )
                    # Wait briefly for nats_worker to process, then verify task state
                    await asyncio.sleep(0.3)
                    orch = self.orchestrator
                    if orch is not None and task_id in orch.tasks:
                        task = orch.tasks[task_id]
                        actual_status = _status_str(task)
                        if actual_status == "paused":
                            return JSONResponse(
                                {"success": True, "task_id": task_id, "status": "paused"}
                            )
                        return JSONResponse(
                            {
                                "success": False,
                                "task_id": task_id,
                                "error": f"Task not paused (status: {actual_status})",
                            },
                            status_code=409,
                        )
                    # Task not in local Orchestrator — assume NATS path will handle it
                    return JSONResponse(
                        {"success": True, "task_id": task_id, "status": "paused", "pending": True}
                    )
                except Exception as e:
                    logger.warning("NATS pause publish failed: %s, falling back", e)

            orch = self.orchestrator
            if orch is None:
                return JSONResponse(
                    {"success": False, "error": "Orchestrator not available"},
                    status_code=503,
                )
            success = orch.pause_task(task_id)
            if success:
                self._record_event("task_pause", task_id=task_id)
                return JSONResponse({"success": True, "task_id": task_id, "status": "paused"})
            return JSONResponse(
                {"success": False, "task_id": task_id, "error": "Task not found or not pausable"},
                status_code=400,
            )

        @app.post("/dashboard/api/tasks/{task_id}/resume")
        async def resume_task_api(task_id: str, request: Request):
            """Resume a paused task."""
            if (resp := self._check_auth(request)) is not None:
                return resp
            # NATS path: publish control event for gRPC server / nats_worker
            if self._nats_publisher is not None:
                try:
                    await self._nats_publisher.publish_event(
                        event_type="task_resume",
                        task_id=task_id,
                    )
                    # Wait briefly for nats_worker to process, then verify task state
                    await asyncio.sleep(0.3)
                    orch = self.orchestrator
                    if orch is not None and task_id in orch.tasks:
                        task = orch.tasks[task_id]
                        actual_status = _status_str(task)
                        if actual_status == "in_progress":
                            return JSONResponse(
                                {"success": True, "task_id": task_id, "status": "in_progress"}
                            )
                        return JSONResponse(
                            {
                                "success": False,
                                "task_id": task_id,
                                "error": f"Task not resumed (status: {actual_status})",
                            },
                            status_code=409,
                        )
                    # Task not in local Orchestrator — assume NATS path will handle it
                    return JSONResponse(
                        {
                            "success": True,
                            "task_id": task_id,
                            "status": "in_progress",
                            "pending": True,
                        }
                    )
                except Exception as e:
                    logger.warning("NATS resume publish failed: %s, falling back", e)

            orch = self.orchestrator
            if orch is None:
                return JSONResponse(
                    {"success": False, "error": "Orchestrator not available"},
                    status_code=503,
                )
            success = orch.resume_task(task_id)
            if success:
                self._record_event("task_resume", task_id=task_id)
                return JSONResponse({"success": True, "task_id": task_id, "status": "in_progress"})
            return JSONResponse(
                {"success": False, "task_id": task_id, "error": "Task not found or not resumable"},
                status_code=400,
            )

        @app.post("/dashboard/api/circuit-breaker/reset")
        async def circuit_breaker_reset_api(request: Request):
            """Reset the circuit breaker to closed state."""
            if (resp := self._check_auth(request)) is not None:
                return resp
            orch = self.orchestrator
            if orch is None:
                return JSONResponse(
                    {"success": False, "error": "Orchestrator not available"},
                    status_code=503,
                )
            success = orch.reset_circuit_breaker()
            if success:
                self._record_event("circuit_breaker_reset")
                return JSONResponse({"success": True, "state": "closed"})
            return JSONResponse(
                {"success": False, "error": "No circuit breaker configured"},
                status_code=400,
            )

        @app.post("/dashboard/api/scheduler/jobs/{job_id}/trigger")
        async def trigger_job_api(job_id: str, request: Request):
            """Manually trigger a scheduled job."""
            if (resp := self._check_auth(request)) is not None:
                return resp
            orch = self.orchestrator
            if orch is None or orch.scheduler is None:
                return JSONResponse(
                    {"success": False, "error": "Scheduler not available"},
                    status_code=503,
                )
            success = orch.scheduler.trigger_job(job_id)
            if success:
                self._record_event("scheduler_trigger", job_id=job_id)
                return JSONResponse({"success": True, "job_id": job_id})
            return JSONResponse(
                {"success": False, "job_id": job_id, "error": "Job not found"},
                status_code=404,
            )

        @app.post("/dashboard/api/tasks/flush-pending")
        async def flush_pending_api(request: Request):
            """Flush all tasks queued during the night window."""
            if (resp := self._check_auth(request)) is not None:
                return resp
            orch = self.orchestrator
            if orch is None:
                return JSONResponse(
                    {"success": False, "error": "Orchestrator not available"},
                    status_code=503,
                )
            count = orch.pending_task_count
            try:
                executed = await orch.flush_pending_tasks()
                self._record_event("flush_pending", count=count, executed=len(executed))
                return JSONResponse(
                    {"success": True, "pending_count": count, "executed": len(executed)}
                )
            except Exception as e:
                logger.error("flush_pending_tasks failed: %s", e, exc_info=True)
                return JSONResponse(
                    {"success": False, "error": f"Flush failed: {e}"},
                    status_code=500,
                )

        # ── Event Log Endpoint ─────────────────────────────────

        @app.get("/dashboard/api/events")
        async def events_api(
            request: Request,
            task_id: Optional[str] = None,  # noqa: UP045
            limit: int = 100,
            offset: int = 0,
        ):
            """Return recent event log entries with pagination.

            Args:
                task_id: Optional filter by task ID.
                limit: Maximum events to return (default: 100, max: 500).
                offset: Number of events to skip from the most recent (default: 0).
            """
            if (resp := self._check_auth(request)) is not None:
                return resp
            # Combine local event log with emitter's recent events
            all_events = list(self._event_log)
            if self.event_emitter is not None:
                all_events = (
                    self.event_emitter.get_recent_events(
                        task_id=task_id,
                        limit=500,
                    )
                    + all_events
                )
            if task_id:
                all_events = [
                    e
                    for e in all_events
                    if e.get("task_id") == task_id or e.get("details", {}).get("task_id") == task_id
                ]
            # Apply pagination
            limit = min(limit, 500)
            offset = max(offset, 0)
            paginated = all_events[offset : offset + limit]
            return JSONResponse(
                {
                    "available": True,
                    "events": paginated,
                    "total": len(all_events),
                    "offset": offset,
                    "limit": limit,
                }
            )

    # ── Data Collection Methods ──────────────────────────────────

    def _record_event(
        self,
        event_type: str,
        **details: Any,
    ) -> None:
        """Append an event to the in-memory event log.

        Args:
            event_type: Type of event (e.g., task_pause, circuit_breaker_reset).
            **details: Additional event details.
        """
        self._event_log.appendleft(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": event_type,
                "details": details,
            }
        )

    def _get_health_data(self) -> dict:
        """Collect engine health data.

        Returns a dict with overall status and per-component details.
        Falls back gracefully if the engine is unavailable.
        """
        orch = self.orchestrator
        if orch is None or orch.engine is None:
            return {
                "available": False,
                "status": "unavailable",
                "components": [],
            }

        try:
            health = orch.engine.health()
            components = []
            for comp in health.components:
                components.append(
                    {
                        "name": comp.name,
                        "status": comp.status,
                        "details": comp.details,
                    }
                )
            return {
                "available": True,
                "status": health.status,
                "version": health.version,
                "uptime_seconds": health.uptime_seconds,
                "components": components,
            }
        except Exception as e:
            logger.warning("Failed to get engine health: %s", e)
            return {
                "available": False,
                "status": "error",
                "error": str(e),
                "components": [],
            }

    def _get_workers_data(self) -> dict:
        """Collect worker list data.

        Returns a dict with worker info and heartbeat status.
        """
        orch = self.orchestrator
        if orch is None:
            return {"available": False, "workers": []}

        heartbeat_timeout = orch.config.heartbeat_timeout_seconds if orch.config else 60
        workers = []
        for wid, w in orch.workers.items():
            now = datetime.now(timezone.utc)
            heartbeat_age = (now - w.last_heartbeat).total_seconds()
            workers.append(
                {
                    "id": w.id,
                    "capabilities": w.capabilities,
                    "current_load": w.current_load,
                    "max_capacity": w.max_capacity,
                    "load_percent": (
                        round(w.current_load / w.max_capacity * 100) if w.max_capacity > 0 else 0
                    ),
                    "last_heartbeat": w.last_heartbeat.isoformat(),
                    "heartbeat_age_seconds": round(heartbeat_age, 1),
                    "heartbeat_stale": heartbeat_age > heartbeat_timeout,
                    "is_available": w.is_available,
                }
            )

        return {
            "available": True,
            "workers": workers,
            "total": len(workers),
            "available_count": sum(1 for w in workers if w["is_available"]),
        }

    def _get_tasks_data(self) -> dict:
        """Collect task status data.

        Returns a dict with active tasks and status counts.
        """
        orch = self.orchestrator
        if orch is None:
            return {"available": False, "tasks": [], "status_counts": {}}

        tasks = []
        status_counts: dict[str, int] = {}
        for tid, t in orch.tasks.items():
            status_val = _status_str(t)
            status_counts[status_val] = status_counts.get(status_val, 0) + 1
            tasks.append(
                {
                    "id": t.id,
                    "description": t.description,
                    "status": status_val,
                    "project_id": t.project_id,
                    "subtask_count": len(t.subtasks),
                    "subtasks": [
                        {
                            "id": st.id,
                            "description": st.description,
                            "status": _status_str(st),
                            "depends_on": st.depends_on,
                        }
                        for st in t.subtasks
                    ],
                    "created_at": t.created_at.isoformat(),
                    "updated_at": t.updated_at.isoformat(),
                }
            )

        pending_count = orch.pending_task_count if hasattr(orch, "pending_task_count") else 0

        return {
            "available": True,
            "tasks": tasks,
            "total": len(tasks),
            "status_counts": status_counts,
            "pending_task_count": pending_count,
        }

    def _get_scheduler_data(self) -> dict:
        """Collect scheduler status data.

        Returns a dict with scheduler running state, night window,
        registered jobs, and recent execution history.
        Falls back gracefully if no scheduler is configured.
        """
        orch = self.orchestrator
        if orch is None or orch.scheduler is None:
            return {"available": False}

        scheduler = orch.scheduler
        try:
            is_running = scheduler.is_running()
        except Exception:
            is_running = False

        # Night window status
        night_window = {
            "active": getattr(orch, "_night_window_active", False),
        }

        # Registered jobs
        jobs = []
        try:
            for job in scheduler.list_jobs():
                job_data = {
                    "id": str(getattr(job, "id", "")),
                    "description": getattr(job, "description", ""),
                    "project_id": getattr(job, "project_id", None),
                    "enabled": getattr(job, "enabled", True),
                }
                cron = getattr(job, "cron_expression", None)
                execute_after = getattr(job, "execute_after", None)
                if cron:
                    job_data["cron_expression"] = cron
                if execute_after:
                    job_data["execute_after"] = str(execute_after)
                jobs.append(job_data)
        except Exception as e:
            logger.warning("Failed to list scheduler jobs: %s", e)

        # Execution history (most recent across all jobs, limited)
        execution_history = []
        try:
            for job in jobs[:10]:
                job_id = job["id"]
                for hist in scheduler.get_execution_history(job_id, limit=5):
                    execution_history.append(
                        {
                            "task_id": job_id,
                            "started_at": str(getattr(hist, "started_at", "")),
                            "completed_at": str(getattr(hist, "completed_at", "")),
                            "status": str(getattr(hist, "status", "")),
                            "result_summary": getattr(hist, "result_summary", None),
                        }
                    )
        except Exception as e:
            logger.warning("Failed to get execution history: %s", e)

        # Sort by started_at descending, keep top 20
        execution_history.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        execution_history = execution_history[:20]

        return {
            "available": True,
            "is_running": is_running,
            "night_window": night_window,
            "jobs": jobs,
            "job_count": len(jobs),
            "execution_history": execution_history,
        }

    def _get_circuit_breaker_data(self, health_data: dict | None = None) -> dict:
        """Collect circuit breaker and rate limiter data.

        Returns a dict with circuit breaker state and rate limiter
        availability. Sources data from the Python-side circuit breaker
        and rate limiter on the Orchestrator, as well as engine health
        component details.
        """
        orch = self.orchestrator
        if orch is None:
            return {
                "circuit_breaker": {
                    "available": False,
                    "state": "Unknown",
                    "failure_count": 0,
                    "total_calls": 0,
                    "total_rejected": 0,
                },
                "rate_limiter": {
                    "available": False,
                    "rpm_available": 0,
                    "tpm_available": 0,
                    "active_count": 0,
                    "total_requests": 0,
                },
                "engine_circuit_breaker": {},
                "engine_rate_limiter": {},
            }

        # Python-side circuit breaker
        cb_data: dict[str, Any] = {
            "available": False,
            "state": "Unknown",
            "failure_count": 0,
            "total_calls": 0,
            "total_rejected": 0,
        }
        if hasattr(orch, "circuit_breaker") and orch.circuit_breaker is not None:
            cb = orch.circuit_breaker
            try:
                cb_data = {
                    "available": True,
                    "state": cb.state.value if hasattr(cb.state, "value") else str(cb.state),
                    "failure_count": cb.failure_count,
                    "total_calls": cb.total_calls,
                    "total_rejected": cb.total_rejected,
                }
            except Exception as e:
                logger.warning("Failed to read circuit breaker: %s", e)
                cb_data["error"] = str(e)

        # Python-side rate limiter
        rl_data: dict[str, Any] = {
            "available": False,
            "rpm_available": 0,
            "tpm_available": 0,
            "active_count": 0,
            "total_requests": 0,
        }
        if hasattr(orch, "rate_limiter") and orch.rate_limiter is not None:
            rl = orch.rate_limiter
            try:
                rl_data = {
                    "available": True,
                    "rpm_available": round(rl.rpm_available, 1),
                    "tpm_available": round(rl.tpm_available, 1),
                    "active_count": rl.active_count,
                    "total_requests": rl.total_requests,
                }
            except Exception as e:
                logger.warning("Failed to read rate limiter: %s", e)
                rl_data["error"] = str(e)

        # Also extract from engine health components for Rust-side metrics
        engine_cb = {}
        engine_rl = {}
        if health_data is None:
            health_data = self._get_health_data()
        if health_data.get("available"):
            for comp in health_data.get("components", []):
                if comp["name"] == "circuit_breaker":
                    engine_cb = comp
                elif comp["name"] == "rate_limiter":
                    engine_rl = comp

        return {
            "circuit_breaker": cb_data,
            "rate_limiter": rl_data,
            "engine_circuit_breaker": engine_cb,
            "engine_rate_limiter": engine_rl,
        }

    def _get_full_snapshot(self) -> dict:
        """Collect a full state snapshot for SSE push.

        Aggregates all panel data into a single dict for efficient
        transmission to the browser.
        """
        health = self._get_health_data()
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "health": health,
            "workers": self._get_workers_data(),
            "tasks": self._get_tasks_data(),
            "scheduler": self._get_scheduler_data(),
            "circuit_breaker": self._get_circuit_breaker_data(health_data=health),
            "events": list(self._event_log),
        }

    # ── Server Lifecycle ─────────────────────────────────────────

    def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Start the dashboard server in a background thread.

        If a NATS client is configured, subscribes to ``uc.task.event``
        for real-time event streaming from the independent NATS consumer.

        Args:
            host: Bind address (default: "0.0.0.0").
            port: Bind port (default: 8080).
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Dashboard is already running")
            return

        config = uvicorn.Config(
            app=self._app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        def _run():
            try:
                self._server.run()
            except Exception as e:
                logger.error("Dashboard server error: %s", e)

        self._thread = threading.Thread(target=_run, daemon=True, name="dashboard")
        self._thread.start()
        logger.info("Dashboard started on http://%s:%d/dashboard/", host, port)

        # Subscribe to NATS uc.task.event if client is available
        self._subscribe_nats_events()

    def _get_nats_event_queue(self) -> asyncio.Queue[dict[str, Any] | None]:
        """Lazily create the NATS event asyncio.Queue on first use.

        Python 3.9 asyncio.Queue() requires a running event loop at creation
        time. If no loop is running (e.g. in synchronous tests), we create
        a new event loop and set it as the current loop for this thread.
        """
        if self._nats_event_queue is None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # ponytail: no running loop — set one so Queue() works on 3.9
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                except Exception:
                    pass
            self._nats_event_queue = asyncio.Queue()
        return self._nats_event_queue

    def _subscribe_nats_events(self) -> None:
        """Register a FastAPI startup event to subscribe to NATS events.

        Uses FastAPI's ``add_event_handler("startup", ...)`` so the
        subscription runs on uvicorn's event loop after the server starts.
        """
        if self._nats_client is None:
            logger.debug("No NATS client configured, skipping event subscription")
            return

        # Schedule the NATS subscription on the server's event loop.
        # The uvicorn server creates its own event loop in the background
        # thread. We use a startup event hook to schedule the subscription
        # once the loop is running.
        async def _subscribe():
            try:
                sub = await self._nats_client.subscribe(
                    NATS_SUBJECT_TASK_EVENT,
                    cb=self._handle_nats_event,
                )
                self._nats_subscriptions.append(sub)
                logger.info("Dashboard subscribed to %s", NATS_SUBJECT_TASK_EVENT)
            except Exception:
                logger.warning(
                    "Failed to subscribe to %s",
                    NATS_SUBJECT_TASK_EVENT,
                    exc_info=True,
                )

        # Use a startup event to schedule the subscription on the
        # uvicorn event loop. This is more reliable than trying to
        # grab the loop from a timer thread.
        if self._nats_client is not None:
            self._app.add_event_handler("startup", _subscribe)

    async def _handle_nats_event(self, msg: Any) -> None:
        """Handle a NATS ``uc.task.event`` message.

        Parses the JSON payload and pushes it into the NATS event queue
        for the SSE stream to pick up.
        """
        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("Invalid NATS event payload: %s", e)
            return

        # Normalize to TaskEvent format
        event_dict: dict[str, Any] = {
            "timestamp": payload.get(
                "timestamp", datetime.now(timezone.utc).isoformat()
            ),
            "type": payload.get("type", "unknown"),
            "task_id": payload.get("task_id", ""),
        }
        if payload.get("subtask_id"):
            event_dict["subtask_id"] = payload.get("subtask_id")
        if payload.get("data"):
            event_dict["data"] = payload.get("data")

        try:
            self._get_nats_event_queue().put_nowait(event_dict)
        except asyncio.QueueFull:
            logger.warning("NATS event queue full, dropping event: %s", event_dict.get("type"))

    def stop(self) -> None:
        """Stop the dashboard server gracefully.

        Unsubscribes from NATS event streams and shuts down the FastAPI server.
        """
        # Clear NATS subscriptions. The actual unsubscribe calls are async
        # and the event loop may be shutting down; since the NATS client
        # will be drained/closed separately, clearing the references is
        # sufficient for cleanup.
        self._nats_subscriptions.clear()

        if self._server is not None:
            self._server.should_exit = True
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Dashboard stopped")

    # ── Factory Methods ─────────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        orchestrator: Any,
        nats_publisher: Any = None,
    ) -> DashboardApp:
        """Create a DashboardApp with NATS client from environment variables.

        If ``UC_NATS_URL`` is set, connects to NATS and creates both
        a NatsPublisher and a raw NATS client for event subscription.
        If not set, creates the Dashboard without NATS (legacy mode).

        Args:
            orchestrator: The Orchestrator instance to monitor.
            nats_publisher: Optional pre-created NatsPublisher. If None
                and UC_NATS_URL is set, one will be created.

        Returns:
            A DashboardApp instance, optionally with NATS integration.

        Note:
            The NATS connection is established synchronously in this
            factory method. For async initialization, connect to NATS
            separately and pass the client to the constructor directly.
        """
        nats_url = os.environ.get("UC_NATS_URL", "")

        if not nats_url:
            logger.info("UC_NATS_URL not set, Dashboard running without NATS")
            return cls(orchestrator, nats_publisher=nats_publisher)

        # Try to connect to NATS synchronously
        nats_client = None
        try:
            import nats as nats_lib

            # Run the async connect in a new event loop
            loop = asyncio.new_event_loop()
            try:
                nats_client = loop.run_until_complete(
                    nats_lib.connect(nats_url, connect_timeout=5.0)
                )
                logger.info("Dashboard connected to NATS at %s", nats_url)
            finally:
                loop.close()
        except ImportError:
            logger.warning(
                "nats-py not installed, Dashboard running without NATS. "
                "Install with: pip install nats-py"
            )
        except Exception as e:
            logger.warning(
                "Failed to connect to NATS at %s: %s. "
                "Dashboard running without NATS event subscription.",
                nats_url,
                e,
            )

        # Create NatsPublisher if we have a client and none was provided
        if nats_client is not None and nats_publisher is None:
            from ultimate_coders.nats_worker import NatsPublisher

            nats_publisher = NatsPublisher(nats_client)

        return cls(
            orchestrator,
            nats_publisher=nats_publisher,
            nats_client=nats_client,
        )
