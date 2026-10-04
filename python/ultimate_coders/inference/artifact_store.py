"""Content-addressed report storage shared across Dashboard and worker hosts."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from ultimate_coders.runtime_state import RuntimeState

PUBLIC_ARTIFACTS = {"report.json", "benchmarks.json", "graph.json", "accepted.patch"}
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


class ArtifactStore:
    def __init__(self, state: RuntimeState):
        self.state = state

    def publish(self, directory: Path) -> dict:
        published = {}
        for name in sorted(PUBLIC_ARTIFACTS):
            path = directory / name
            if not path.is_file():
                continue
            if path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise ValueError("Public artifact exceeds 16 MiB")
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            self.state.mutate(
                "artifact_blobs",
                digest,
                lambda old: (
                    old
                    or {
                        "sha256": digest,
                        "size": len(content),
                        "content": base64.b64encode(content).decode(),
                        "state": "published",
                    }
                ),
            )
            published[name] = {
                "artifact_id": digest,
                "sha256": digest,
                "size": len(content),
                "location": "runtime-store",
            }
        return published

    def read(self, metadata: dict) -> bytes:
        digest = metadata.get("artifact_id")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid artifact identity")
        if metadata.get("sha256") != digest or not isinstance(metadata.get("size"), int):
            raise ValueError("Invalid artifact metadata")
        record = self.state.get("artifact_blobs", digest)
        if not record:
            raise FileNotFoundError("Artifact publication is unavailable")
        try:
            content = base64.b64decode(record["content"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Artifact content is malformed") from exc
        if (
            len(content) > MAX_ARTIFACT_BYTES
            or len(content) != metadata["size"]
            or len(content) != record.get("size")
            or hashlib.sha256(content).hexdigest() != digest
            or record.get("sha256") != digest
            or record.get("state") != "published"
        ):
            raise ValueError("Artifact integrity check failed")
        return content
