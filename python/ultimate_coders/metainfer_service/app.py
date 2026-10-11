"""HTTP contract and workspace challenges for the contained MetaInfer launcher."""

from __future__ import annotations

import hashlib
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, StrictStr

from ultimate_coders.inference.service_contract import CONTRACT_VERSION


@dataclass(frozen=True)
class ServiceConfig:
    control_dir: Path
    workspace_roots: tuple[Path, ...]
    backend_id: str
    revision: str

    def __post_init__(self) -> None:
        # Keep the startup authorization stable even if a job retargets a
        # symlink used by the operator to name a workspace root.
        object.__setattr__(
            self, "workspace_roots", tuple(root.resolve() for root in self.workspace_roots)
        )

    def workspace(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("Workspace must be absolute")
        root = path.resolve(strict=True)
        if not root.is_dir() or not any(
            root == allowed or allowed in root.parents for allowed in self.workspace_roots
        ):
            raise ValueError("Workspace is outside configured roots")
        return root


class WorkspaceProbe(BaseModel):
    workspace: StrictStr
    read_path: StrictStr
    write_path: StrictStr
    write_token: StrictStr


def add_contract_routes(app: FastAPI, config: ServiceConfig, jobs: Any) -> None:
    @app.get("/api/uc/contract")
    def contract():
        return {
            "contract_version": CONTRACT_VERSION,
            "backend_id": config.backend_id,
            "revision": config.revision,
            "capabilities": ["workspace_probe"] + (["quiescence"] if jobs else []),
        }

    @app.post("/api/uc/workspaces/verify")
    def verify(probe: WorkspaceProbe):
        try:
            root = config.workspace(probe.workspace)
            source, target = Path(probe.read_path), Path(probe.write_path)
            if not source.is_absolute() or not target.is_absolute():
                raise ValueError("Probe paths must be absolute")
            source = source.resolve(strict=True)
            parent = target.parent.resolve(strict=True)
            if root not in source.parents or (parent != root and root not in parent.parents):
                raise ValueError("Probe files must belong to the requested workspace")
            if not source.is_file() or source.stat().st_size > 1024 * 1024:
                raise ValueError("Invalid probe source")
            token = probe.write_token.encode("utf-8")
            if not token or len(token) > 4096:
                raise ValueError("Invalid probe token")
            if jobs is not None:
                return jobs.workspace_probe(root, source, parent / target.name, probe.write_token)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(parent / target.name, flags, 0o600)
            with os.fdopen(descriptor, "wb") as writer:
                writer.write(token)
                writer.flush()
                os.fsync(writer.fileno())
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"read_sha256": digest, "writer_identity": f"{socket.gethostname()}:{os.getpid()}"}

    @app.get("/api/uc/jobs/{task_id}/quiescence")
    def quiescence(task_id: str):
        if jobs is None:
            raise HTTPException(503, "Contained launcher unavailable")
        try:
            return jobs.quiescence(task_id)
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/uc/hardware")
    def hardware():
        if jobs is None:
            raise HTTPException(503, "Contained launcher unavailable")
        try:
            return jobs.hardware()
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(503, str(exc)) from exc
