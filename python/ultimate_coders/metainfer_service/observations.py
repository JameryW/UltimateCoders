"""Small UC observation surface; never ask host readers to follow job paths."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException


def read_json(root: Path, name: str, default: Any) -> Any:
    """Open a single fixed metadata file relative to a no-follow directory FD."""
    if Path(name).name != name or name in (".", ".."):
        raise ValueError("Invalid metadata name")
    if os.name != "posix":
        raise ValueError("Contained metadata reads require local Linux")
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return default
    try:
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return default
        with os.fdopen(descriptor, "rb") as reader:
            info = os.fstat(reader.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
                raise ValueError("Metadata must be a bounded regular file")
            return json.loads(reader.read(16 * 1024 * 1024 + 1))
    finally:
        os.close(directory)


def contain_observations(app: FastAPI, launcher: Any, shell: APIRouter) -> None:
    from metainfer.server import paths, tasks

    # Upstream task/plugin readers accept job-owned absolute log paths and
    # symlinks. Keep discovery/create/control, replace observation with fixed
    # no-follow files, and omit optional plugin/log endpoints (HTTP 404).
    allowed = {
        ("/task-types", "GET"),
        ("/task-types/{task_type}/schema", "GET"),
        ("/tasks", "POST"),
        ("/{task_id}/control", "POST"),
    }
    # Filter the public router BEFORE include_router. New FastAPI versions
    # retain included routers as wrapper objects rather than flattened routes.
    shell.routes = [
        route
        for route in shell.routes
        if all((route.path, method) in allowed for method in route.methods)
    ]
    app.include_router(shell, prefix="/api/sys-shell")

    def entry_view(entry):
        return {
            "id": entry.id,
            "type": entry.type,
            "label": entry.label,
            "state_dir": str(paths.task_dir(entry.id)),
            "created_at": entry.created_at,
            "launcher": entry.launcher,
            "status": launcher.status(entry.id).to_dict(),
        }

    @app.get("/api/sys-shell/tasks")
    def list_tasks():
        return {"tasks": [entry_view(entry) for entry in tasks.list_tasks()]}

    @app.get("/api/sys-shell/{task_id}")
    def get_task(task_id: str):
        entry = tasks.get_task(task_id)
        if entry is None:
            raise HTTPException(404, "Unknown task")
        state = paths.task_dir(entry.id)
        try:
            requirements, run = (
                read_json(state, "requirements.json", None),
                read_json(state, "run.json", {}),
            )
            if not isinstance(run, dict) or (
                requirements is not None and not isinstance(requirements, dict)
            ):
                raise ValueError("Invalid task metadata")
        except (ValueError, OSError) as exc:
            raise HTTPException(503, "Task metadata is unreadable or unsafe") from exc
        return {**entry_view(entry), "requirements": requirements, "run": run}
