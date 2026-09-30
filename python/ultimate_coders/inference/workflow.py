"""Bounded optimization with benchmark evidence and candidate transactions."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Awaitable
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .benchmark import BenchmarkRunner
from .graph import ExecutionAdaptationGraph
from .models import MetaInferResult, MetaInferTask
from .oracle import Oracle

Candidate = Callable[[MetaInferTask], Awaitable[MetaInferResult]]


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Inference workspace git operation failed: {args[0]}")
    return result.stdout


class WorkspaceTransaction:
    """Snapshot tracked and nonignored code in an exclusively owned workspace.

    Ignored build caches/weights are not rollback-managed. Backend commits or
    submodule edits cannot be accepted. Never reset HEAD or clean a shared repo.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.head = _git(self.root, "rev-parse", "HEAD")
        self.files: dict[str, tuple[bytes | str, int]] = {}
        size = 0
        for name in self._names():
            path = self._path(name)
            if path.is_dir() and not path.is_symlink():
                raise ValueError("Optimization workspaces with git submodules are unsupported")
            if not path.exists() and not path.is_symlink():
                continue
            mode = path.lstat().st_mode
            content = os.readlink(path) if stat.S_ISLNK(mode) else path.read_bytes()
            size += len(content)
            if size > 100 * 1024 * 1024:
                raise ValueError("Rollback snapshot exceeds 100 MiB; use a smaller code workspace")
            self.files[name] = (content, mode)

    def _names(self) -> set[str]:
        raw = _git(self.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        return {os.fsdecode(name) for name in raw.split(b"\0") if name}

    def _path(self, name: str) -> Path:
        path = self.root / name
        # Resolve the parent, not a symlink leaf which can safely be unlinked.
        path.parent.resolve().relative_to(self.root)
        if name == ".git" or name.startswith((".git/", ".git\\")):
            raise ValueError("Refusing to modify git metadata")
        return path

    def verify_head(self) -> None:
        if _git(self.root, "rev-parse", "HEAD") != self.head:
            raise RuntimeError("Backend changed HEAD; candidate cannot be accepted")

    def restore(self) -> None:
        self.verify_head()
        # The initial index was clean; accepted patches remain unstaged.
        _git(self.root, "restore", "--staged", "--source=HEAD", "--", ".")
        for name, (content, mode) in self.files.items():
            # Remove obstructing ancestors without following candidate symlinks.
            parts = Path(name).parts
            parent = self.root
            for part in parts[:-1]:
                parent = parent / part
                if parent.is_symlink() or parent.is_file():
                    parent.unlink()
                parent.mkdir(exist_ok=True)
            path = self._path(name)
            if path.is_symlink():
                path.unlink()
            if path.is_dir():
                # _path verifies this owned target lies within the worktree.
                shutil.rmtree(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                if path.exists():
                    path.unlink()
                path.symlink_to(content)
            else:
                path.write_bytes(content)
                path.chmod(stat.S_IMODE(mode))
        # Original ignore rules are now restored before enumerating additions.
        for name in self._names() - self.files.keys():
            path = self._path(name)
            if path.is_symlink() or path.is_file():
                path.unlink()


class OptimizationWorkflow:
    def __init__(self, oracle: Oracle) -> None:
        self.oracle = oracle

    async def run(
        self,
        task: MetaInferTask,
        benchmark: BenchmarkRunner,
        candidate: Candidate,
        artifact_dir: str,
        *,
        max_iterations: int = 1,
    ) -> dict:
        if type(max_iterations) is not int or not 1 <= max_iterations <= 50:
            raise ValueError("max_iterations must be between 1 and 50")
        root = Path(task.repository).resolve()
        git_root = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
        if root != git_root or _git(root, "status", "--porcelain"):
            raise ValueError("Optimization requires an exclusively owned clean git worktree")
        artifacts = Path(artifact_dir).resolve()
        if artifacts == root or root in artifacts.parents:
            raise ValueError("Experiment artifacts must be outside the candidate workspace")
        artifacts.mkdir(parents=True, exist_ok=True)
        benchmark.protect(str(root))
        initial = await benchmark.measure(str(root), baseline=True)
        invalid = self.oracle.validate(initial)
        if invalid:
            raise ValueError("Invalid baseline: " + "; ".join(invalid))
        best, accepted = initial, False
        history = []
        graph = ExecutionAdaptationGraph.for_task(task)
        last_backend: MetaInferResult | None = None

        def save() -> None:
            (artifacts / "graph.json").write_text(
                json.dumps(graph.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (artifacts / "benchmarks.json").write_text(
                json.dumps(
                    {
                        "baseline": initial.to_dict(),
                        "optimized": best.to_dict(),
                        "iterations": history,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        for index in range(1, max_iterations + 1):
            transaction = WorkspaceTransaction(root)
            request = replace(
                task,
                constraints={
                    **task.constraints,
                    "oracle_feedback": {
                        "best": best.to_dict(),
                        "previous_verdict": history[-1].get("verdict") if history else None,
                    },
                    "execution_rules": "Edit only nonignored code in repository. Do not commit, "
                    "change benchmark harnesses, git metadata, submodules or external files.",
                },
            )
            keep = False
            try:
                last_backend = await candidate(request)
                if last_backend.status != "completed":
                    raise RuntimeError("Candidate backend did not complete")
                transaction.verify_head()
                benchmark.verify_harness()
                measured = await benchmark.measure(str(root))
                verdict = self.oracle.evaluate(best, measured)
                record = {
                    "index": index,
                    "backend": last_backend.to_dict(),
                    "measurement": measured.to_dict(),
                    "verdict": verdict.to_dict(),
                }
                history.append(record)
                graph.record_evidence(
                    index,
                    {
                        "task_id": last_backend.task_id,
                        "benchmark": measured.to_dict(),
                        "verdict": verdict.to_dict(),
                    },
                )
                keep = verdict.accepted
                if keep:
                    # Keep code changes, but never let the backend stage a later commit.
                    _git(root, "restore", "--staged", "--source=HEAD", "--", ".")
                    best, accepted = measured, True
            except BaseException as exc:
                history.append({"index": index, "error": type(exc).__name__, "accepted": False})
                raise
            finally:
                if not keep:
                    transaction.restore()
                save()
        patch = os.fsdecode(_git(root, "diff", "--binary", "HEAD"))
        # git diff omits untracked files. Model porting commonly creates them.
        added = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
        for name in added.split(b"\0"):
            if not name:
                continue
            diff = subprocess.run(
                ["git", "diff", "--no-index", "--binary", "--", "/dev/null", os.fsdecode(name)],
                cwd=root,
                capture_output=True,
                check=False,
            )
            if diff.returncode not in (0, 1):
                raise RuntimeError("Could not capture newly created candidate file")
            patch += os.fsdecode(diff.stdout)
        (artifacts / "accepted.patch").write_text(patch, encoding="utf-8")
        changes = []
        names = _git(root, "diff", "--name-status", "--no-renames", "-z", "HEAD").split(b"\0")
        for i in range(0, len(names) - 1, 2):
            changes.append(
                {
                    "file_path": os.fsdecode(names[i + 1]),
                    "change_type": {b"A": "created", b"D": "deleted"}.get(names[i], "modified"),
                }
            )
        changes.extend(
            {"file_path": os.fsdecode(name), "change_type": "created"}
            for name in added.split(b"\0")
            if name
        )
        return {
            "success": accepted,
            "task_type": task.task_type.value,
            "baseline": initial.to_dict(),
            "optimized": best.to_dict(),
            "iterations": history,
            "graph": graph.to_dict(),
            "patch": patch,
            "file_changes": changes,
            "artifacts": {
                "graph": str(artifacts / "graph.json"),
                "benchmark": str(artifacts / "benchmarks.json"),
                "patch": str(artifacts / "accepted.patch"),
            },
            "backend": last_backend.to_dict() if last_backend else None,
        }
