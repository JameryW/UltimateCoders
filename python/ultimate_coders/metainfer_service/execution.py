"""Publish pinned evolve-kernel candidates inside their existing OCI writer scope."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import stat
import subprocess
import sys
import uuid
from pathlib import Path

from .observations import read_json

logger = logging.getLogger(__name__)


def _target_parts(repository: Path, kernel: Path) -> tuple[str, ...]:
    if not repository.is_absolute() or not kernel.is_absolute():
        raise ValueError("Repository and kernel must be absolute")
    parts = kernel.relative_to(repository).parts
    if not parts or any(part in (".", "..") or part.casefold() == ".git" for part in parts):
        raise ValueError("Kernel target must stay outside Git metadata")
    return parts


def _parent(repository: Path, parts: tuple[str, ...]) -> int:
    directory = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory
    except BaseException:
        os.close(directory)
        raise


def _replace(directory: int, name: str, content: bytes, mode: int = 0o644) -> None:
    temporary = ".uc-publication-" + uuid.uuid4().hex
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as writer:
            os.fchmod(writer.fileno(), mode)
            writer.write(content)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _write_run(state: Path, run: dict) -> None:
    directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _replace(directory, "run.json", json.dumps(run, ensure_ascii=False).encode())
    finally:
        os.close(directory)


def publish_kernel(repository: Path, state: Path, generated: Path, kernel: Path) -> dict:
    """Copy the upstream-ranked code, leaving independent Oracle acceptance pending."""
    parts = _target_parts(repository, kernel)
    run = read_json(state, "run.json", None)
    if (
        not isinstance(run, dict)
        or run.get("finished") is not True
        or run.get("final_status") not in ("success", "completed", "succeeded")
    ):
        raise ValueError("Publication requires a successful terminal upstream run")
    library = read_json(generated, "kernel_library.json", None)
    if not isinstance(library, list) or not 1 <= len(library) <= 10:
        raise ValueError("Publication requires a measured kernel library")
    # Approx prune pre-filter (ticket 08): when enforce+armed, drop clearly-worse
    # library entries by exec_time_ms before taking the ranked best. Soft no-op
    # when the latch is off or scores are missing — never inside Oracle.evaluate.
    from ultimate_coders.inference.approx_eval import filter_ranked_library

    library = filter_ranked_library(library, score_key="exec_time_ms", higher_is_better=False)
    if not library:
        raise ValueError("Publication requires a measured kernel library")
    selected = library[0]  # Ranked best first (upstream order, or approx survivors).
    elapsed = selected.get("exec_time_ms") if isinstance(selected, dict) else None
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed <= 0
    ):
        raise ValueError("Publication requires a measured kernel")
    code, identity = selected.get("code"), selected.get("id")
    if (
        not isinstance(code, str)
        or not code.strip()
        or len(code.encode()) > 4 * 1024 * 1024
        or not isinstance(identity, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", identity)
    ):
        raise ValueError("Invalid ranked kernel code or identity")
    content = code.encode()
    try:
        directory = _parent(repository, parts)
        try:
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Kernel target must be a regular file")
            finally:
                os.close(descriptor)
            _replace(directory, parts[-1], content, stat.S_IMODE(info.st_mode) & 0o777)
        finally:
            os.close(directory)
    except OSError as exc:
        raise ValueError("Kernel target must stay in the owned directory tree") from exc
    receipt = {
        "kernel_path": Path(*parts).as_posix(),
        "candidate_id": identity,
        "code_sha256": hashlib.sha256(content).hexdigest(),
        "publication_kind": "oci_generated_candidate",
        "oracle_acceptance": "pending",
    }
    _write_run(state, {**run, "uc_candidate": receipt})
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-module", required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--generated-dir", type=Path, required=True)
    args = parser.parse_args()
    requirements = read_json(args.requirements.parent, args.requirements.name, None)
    if not isinstance(requirements, dict) or requirements.get("task_type") != "evolve-kernel":
        raise ValueError("Only the pinned evolve-kernel publisher is supported")
    if args.upstream_module != "metainfer.tasks.evolve_kernel.orchestrator.cli":
        raise ValueError("Unsupported upstream execution module")
    kernel = Path(requirements["kernel_file_path"])
    _target_parts(args.repository, kernel)
    code = subprocess.call(
        [
            sys.executable,
            "-m",
            args.upstream_module,
            "run",
            str(args.requirements),
            "--state-dir",
            str(args.state_dir),
            "--workspace-dir",
            str(args.generated_dir),
        ]
    )
    try:
        if code:
            raise ValueError("Upstream execution failed")
        publish_kernel(args.repository, args.state_dir, args.generated_dir, kernel)
    except (OSError, ValueError, KeyError, TypeError):
        logger.error("Kernel candidate publication failed", exc_info=True)
        run = read_json(args.state_dir, "run.json", {})
        _write_run(
            args.state_dir,
            {
                **(run if isinstance(run, dict) else {}),
                "finished": True,
                "final_status": "failed",
                "error": "Kernel candidate publication failed",
            },
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
