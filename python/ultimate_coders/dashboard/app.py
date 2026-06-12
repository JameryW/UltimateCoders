"""DashboardApp — FastAPI application for the monitoring dashboard.

Provides REST API endpoints and SSE stream for real-time cluster
monitoring. Embedded in the Orchestrator process for zero-latency
access to in-memory state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

logger = logging.getLogger(__name__)

# Resolve template and static directories relative to this file
_DASHBOARD_DIR = Path(__file__).parent
_TEMPLATES_DIR = _DASHBOARD_DIR / "templates"
_STATIC_DIR = _DASHBOARD_DIR / "static"


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
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Configure FastAPI routes and middleware."""
        app = self._app

        # CORS middleware — allows CDN scripts (Tailwind) and
        # cross-origin SSE clients to work without errors.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET"],
            allow_headers=["*"],
        )

        # Mount static files
        if _STATIC_DIR.exists():
            app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        # Jinja2 templates
        templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

        @app.get("/dashboard/", response_class=HTMLResponse)
        async def dashboard_page(request: Request):
            """Render the main dashboard HTML page."""
            return templates.TemplateResponse(request, "index.html", {})

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
            """SSE endpoint: push full state snapshot every 5 seconds."""
            from sse_starlette.sse import EventSourceResponse

            async def event_generator():
                while True:
                    if await request.is_disconnected():
                        break
                    snapshot = self._get_full_snapshot()
                    yield {
                        "event": "update",
                        "data": json.dumps(snapshot),
                    }
                    await asyncio.sleep(5)

            return EventSourceResponse(event_generator())

    # ── Data Collection Methods ──────────────────────────────────

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
                components.append({
                    "name": comp.name,
                    "status": comp.status,
                    "details": comp.details,
                })
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
            workers.append({
                "id": w.id,
                "capabilities": w.capabilities,
                "current_load": w.current_load,
                "max_capacity": w.max_capacity,
                "load_percent": round(w.current_load / w.max_capacity * 100) if w.max_capacity > 0 else 0,
                "last_heartbeat": w.last_heartbeat.isoformat(),
                "heartbeat_age_seconds": round(heartbeat_age, 1),
                "heartbeat_stale": heartbeat_age > heartbeat_timeout,
                "is_available": w.is_available,
            })

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
            tasks.append({
                "id": t.id,
                "description": t.description,
                "status": status_val,
                "project_id": t.project_id,
                "subtask_count": len(t.subtasks),
                "created_at": t.created_at.isoformat(),
                "updated_at": t.updated_at.isoformat(),
            })

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
                    execution_history.append({
                        "task_id": job_id,
                        "started_at": str(getattr(hist, "started_at", "")),
                        "completed_at": str(getattr(hist, "completed_at", "")),
                        "status": str(getattr(hist, "status", "")),
                        "result_summary": getattr(hist, "result_summary", None),
                    })
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
                "circuit_breaker": {"available": False},
                "rate_limiter": {"available": False},
                "engine_circuit_breaker": {},
                "engine_rate_limiter": {},
            }

        # Python-side circuit breaker
        cb_data: dict[str, Any] = {"available": False}
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
                cb_data = {"available": False, "error": str(e)}

        # Python-side rate limiter
        rl_data: dict[str, Any] = {"available": False}
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
                rl_data = {"available": False, "error": str(e)}

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
