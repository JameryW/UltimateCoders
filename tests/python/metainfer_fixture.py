"""UC extension responses for fault services; not real GPU proof evidence."""

import hashlib
from pathlib import Path


def contract_response(path, body=None, backend_id="fixture-backend"):
    if path == "/api/uc/contract":
        return {
            "contract_version": "uc-metainfer/1",
            "backend_id": backend_id,
            "revision": "b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890",
            "capabilities": ["workspace_probe", "quiescence"],
        }
    if path == "/api/uc/workspaces/verify":
        Path(body["write_path"]).write_text(body["write_token"])
        return {
            "read_sha256": hashlib.sha256(Path(body["read_path"]).read_bytes()).hexdigest(),
            "writer_identity": "fault-service",
        }
    if path.startswith("/api/uc/jobs/") and path.endswith("/quiescence"):
        return {
            "contract_version": "uc-metainfer/1",
            "backend_id": backend_id,
            "task_id": path.split("/")[-2],
            "writers_stopped": True,
            "proof_kind": "cgroup_empty",
            "execution_scope": "fixture-only",
            "evidence_id": "fault-service-stop-receipt",
        }
    return None
