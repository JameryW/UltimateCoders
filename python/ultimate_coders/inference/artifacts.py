"""Artifact location shared by the runner, sandbox and Dashboard."""

from __future__ import annotations

import os
from pathlib import Path


def artifact_root(project_path: str | None = None) -> Path:
    configured = os.environ.get("UC_INFERENCE_ARTIFACT_DIR")
    project = Path(project_path or os.environ.get("UC_PROJECT_PATH") or os.getcwd()).resolve()
    return Path(configured).resolve() if configured else project.parent / ".uc-inference-artifacts"
