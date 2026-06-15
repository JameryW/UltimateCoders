"""DashboardApp — FastAPI application for the monitoring dashboard.

Provides REST API endpoints and SSE stream for real-time cluster
monitoring. Embedded in the Orchestrator process for zero-latency
access to in-memory state.

The frontend is a separate React SPA (dashboard/ directory) that
connects to these API endpoints. Jinja2 templates and static file
serving have been removed — the SPA is built and deployed independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class DashboardApp:
    """FastAPI-based monitoring dashboard embedded in the Orchestrator.

    Reads Orchestrator state directly from memory (workers, tasks,
    scheduler) and engine health via PyO3/Rust. Pushes updates to
    the browser via SSE every 5 seconds.

    Usage:
        app = DashboardApp(orchestrator)
        app.start(host="0.0.0.0", port=8080)
        # ...
        app.stop()
    """

    def __init__(self, orchestrator: Any) -> None:
        """Create the dashboard app.

        Args:
            orchestrator: The Orchestrator instance to monitor.
        """
        self.orchestrator = orchestrator
        self._app = FastAPI(title="UltimateCoders Dashboard")
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._event_log: deque[dict[str, Any]] = deque(maxlen=200)
        # Connect to Orchestrator's event emitter if available
        self.event_emitter = getattr(orchestrator, "event_emitter", None) if orchestrator else None
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Configure FastAPI routes and middleware."""
        app = self._app

        # CORS middleware — allows CDN scripts (Tailwind) and
        # cross-origin SSE clients to work without errors.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

        # Mount static files — REMOVED: frontend is now a separate React SPA
        # Static files and Jinja2 templates are no longer served by the backend

        # API endpoints

        @app.get("/dashboard/api/health")
        async def health_api():
            """Return engine health JSON."""
            return JSONResponse(self._get_health_data())

        @app.get("/dashboard/api/workers")
        async def workers_api():
            """Return worker list JSON."""
            return JSONResponse(self._get_workers_data())

        @app.get("/dashboard/api/tasks")
        async def tasks_api():
            """Return task status JSON."""
            return JSONResponse(self._get_tasks_data())

        @app.get("/dashboard/api/scheduler")
        async def scheduler_api():
            """Return scheduler status JSON."""
            return JSONResponse(self._get_scheduler_data())

        @app.get("/dashboard/api/circuit-breaker")
        async def circuit_breaker_api():
            """Return circuit breaker and rate limiter JSON."""
            return JSONResponse(self._get_circuit_breaker_data())

        @app.get("/dashboard/api/stream")
        async def stream(request: Request):
            """SSE endpoint: push real-time events + periodic full snapshots.

            Uses a hybrid approach:
            - Immediate task events (from TaskEventEmitter) via 'task_event' SSE type
            - Full state snapshot every 5 seconds via 'update' SSE type
            """
            from sse_starlette.sse import EventSourceResponse

            async def event_generator():
                while True:
                    if await request.is_disconnected():
                        break

                    # Try to get a real-time event first (waits up to 5s)
                    if self.event_emitter is not None:
                        event = await self.event_emitter.wait_for_event(timeout=5.0)
                        if event is not None:
                            # Also record in our local event log
                            self._event_log.appendleft(event.to_dict())
                            yield {
                                "event": "task_event",
                                "data": json.dumps(event.to_dict()),
                            }
                            continue  # Check for more events immediately

                    # Timeout or no emitter: send full snapshot then wait
                    snapshot = self._get_full_snapshot()
                    yield {
                        "event": "update",
                        "data": json.dumps(snapshot),
                    }
                    # When no emitter is available, we must sleep to avoid
                    # spinning the loop without any delay (infinite fast loop).
                    if self.event_emitter is None:
                        await asyncio.sleep(5)

            return EventSourceResponse(event_generator())

        # ── Task Submit Endpoint ─────────────────────────────────

        @app.post("/dashboard/api/tasks/submit")
        async def submit_task_api(request: Request):
            """Submit a new code development task to the Orchestrator.

            Accepts a JSON body with:
            - description (required): Task description.
            - project_id (optional): Project/repository context.

            Returns the created task with its ID and subtask list.
            """
            orch = self.orchestrator
            if orch is None:
                return JSONResponse(
                    {"success": False, "error": "Orchestrator not available"},
                    status_code=503,
                )
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

            try:
                task = await orch.submit_task(description, project_id=project_id)
            except Exception as e:
                logger.error("Failed to submit task: %s", e, exc_info=True)
                return JSONResponse(
                    {"success": False, "error": str(e)},
                    status_code=500,
                )

            # Record in event log (the Orchestrator already emits via
            # event_emitter in submit_task, so we don't duplicate it here)
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
        async def pause_task_api(task_id: str):
            """Pause a running task."""
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
        async def resume_task_api(task_id: str):
            """Resume a paused task."""
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
        async def circuit_breaker_reset_api():
            """Reset the circuit breaker to closed state."""
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
        async def trigger_job_api(job_id: str):
            """Manually trigger a scheduled job."""
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
        async def flush_pending_api():
            """Flush all tasks queued during the night window."""
            orch = self.orchestrator
            if orch is None:
                return JSONResponse(
                    {"success": False, "error": "Orchestrator not available"},
                    status_code=503,
                )
            count = orch.pending_task_count
            # flush_pending_tasks is async, but in a sync context we
            # return the count and let the Orchestrator handle it
            self._record_event("flush_pending", count=count)
            return JSONResponse({"success": True, "pending_count": count})

        # ── Event Log Endpoint ─────────────────────────────────

        @app.get("/dashboard/api/events")
        async def events_api(task_id: Optional[str] = None, limit: int = 100):  # noqa: UP045
            """Return recent event log entries.

            Args:
                task_id: Optional filter by task ID.
                limit: Maximum events to return (default: 100).
            """
            # Combine local event log with emitter's recent events
            all_events = list(self._event_log)
            if self.event_emitter is not None:
                all_events = (
                    self.event_emitter.get_recent_events(
                        task_id=task_id,
                        limit=limit,
                    )
                    + all_events
                )
            if task_id:
                # Filter by task_id: emitter events have top-level task_id,
                # local _event_log events have it inside details dict
                all_events = [
                    e
                    for e in all_events
                    if e.get("task_id") == task_id or e.get("details", {}).get("task_id") == task_id
                ]
            return JSONResponse(
                {
                    "available": True,
                    "events": all_events[:limit],
                    "total": len(all_events),
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
            status_val = t.status.value if hasattr(t.status, "value") else str(t.status)
            status_counts[status_val] = status_counts.get(status_val, 0) + 1
            tasks.append(
                {
                    "id": t.id,
                    "description": t.description,
                    "status": status_val,
                    "project_id": t.project_id,
                    "subtask_count": len(t.subtasks),
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

    def stop(self) -> None:
        """Stop the dashboard server gracefully."""
        if self._server is not None:
            self._server.should_exit = True
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Dashboard stopped")
