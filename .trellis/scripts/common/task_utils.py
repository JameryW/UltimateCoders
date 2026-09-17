#!/usr/bin/env python3
"""
Task utility functions.

Provides:
    is_safe_task_path   - Validate task path is safe to operate on
    find_task_by_name   - Find task directory by name
    resolve_task_dir    - Resolve task directory from name, relative, or absolute path
    archive_task_dir    - Archive task to monthly directory
    run_task_hooks      - Run lifecycle hooks for task events
"""

from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .paths import DIR_ARCHIVE, DIR_TASKS, DIR_WORKFLOW, get_repo_root, get_tasks_dir


# =============================================================================
# Path Safety
# =============================================================================

def is_safe_task_path(task_path: str, repo_root: Path | None = None) -> bool:
    """Check if a relative task path is safe to operate on.

    Args:
        task_path: Task path (relative to repo_root).
        repo_root: Repository root path. Defaults to auto-detected.

    Returns:
        True if safe, False if dangerous.
    """
    if repo_root is None:
        repo_root = get_repo_root()

    normalized = task_path.replace("\\", "/")

    # Check empty or null
    if not normalized or normalized == "null":
        print("Error: empty or null task path", file=sys.stderr)
        return False

    # Reject absolute paths
    if Path(task_path).is_absolute():
        print(f"Error: absolute path not allowed: {task_path}", file=sys.stderr)
        return False

    # Reject ".", "..", paths starting with "./" or "../", or containing ".."
    if normalized in (".", "..") or normalized.startswith("./") or normalized.startswith("../") or ".." in normalized:
        print(f"Error: path traversal not allowed: {task_path}", file=sys.stderr)
        return False

    # Final check: ensure resolved path is not the repo root
    abs_path = repo_root / Path(normalized)
    if abs_path.exists():
        try:
            resolved = abs_path.resolve()
            root_resolved = repo_root.resolve()
            if resolved == root_resolved:
                print(f"Error: path resolves to repo root: {task_path}", file=sys.stderr)
                return False
        except (OSError, IOError):
            pass

    return True


# =============================================================================
# Task Lookup
# =============================================================================

def find_task_by_name(task_name: str, tasks_dir: Path) -> Path | None:
    """Find task directory by name (exact or suffix match).

    Args:
        task_name: Task name to find.
        tasks_dir: Tasks directory path.

    Returns:
        Absolute path to task directory, or None if not found.
    """
    if not task_name or not tasks_dir or not tasks_dir.is_dir():
        return None

    # Try exact match first
    exact_match = tasks_dir / task_name
    if exact_match.is_dir():
        return exact_match

    # Try suffix match (e.g., "my-task" matches "01-21-my-task")
    for d in tasks_dir.iterdir():
        if d.is_dir() and d.name.endswith(f"-{task_name}"):
            return d

    return None


# =============================================================================
# Archive Operations
# =============================================================================

def archive_task_dir(task_dir_abs: Path, repo_root: Path | None = None) -> Path | None:
    """Archive a task directory to archive/{YYYY-MM}/.

    Args:
        task_dir_abs: Absolute path to task directory.
        repo_root: Repository root path. Defaults to auto-detected.

    Returns:
        Path to archived directory, or None on error.
    """
    if not task_dir_abs.is_dir():
        print(f"Error: task directory not found: {task_dir_abs}", file=sys.stderr)
        return None

    if repo_root is None:
        repo_root = get_repo_root()

    # Get tasks directory (parent of the task)
    tasks_dir = task_dir_abs.parent
    archive_dir = tasks_dir / "archive"
    year_month = datetime.now().strftime("%Y-%m")
    month_dir = archive_dir / year_month

    # Create archive directory
    try:
        month_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, IOError) as e:
        print(f"Error: Failed to create archive directory: {e}", file=sys.stderr)
        return None

    # Move task to archive
    task_name = task_dir_abs.name
    dest = month_dir / task_name

    try:
        shutil.move(str(task_dir_abs), str(dest))
    except (OSError, IOError, shutil.Error) as e:
        print(f"Error: Failed to move task to archive: {e}", file=sys.stderr)
        return None

    # The move breaks every citation the task makes to its own files. Repoint
    # them in the same operation, or the corpus silently accumulates dangling
    # paths (#678). Done AFTER the move so that a failure here leaves the
    # pre-existing failure mode rather than inventing a worse one.
    for rel_path, count in sorted(rewrite_archived_task_refs(dest, repo_root).items()):
        print(f"[OK] Repointed {count} citation(s) in {rel_path}", file=sys.stderr)

    old_rel = pre_archive_task_ref(dest, repo_root)
    if old_rel:
        external = find_external_task_refs(repo_root, old_rel, skip_dir=dest)
        if external:
            print(
                f"[WARN] {len(external)} other file(s) still cite {old_rel}; "
                "repoint them deliberately -- this archive did not touch them:",
                file=sys.stderr,
            )
            for rel_path in external[:10]:
                print(f"       {rel_path}", file=sys.stderr)

    return dest


# =============================================================================
# Archived-task reference repair
# =============================================================================

# Suffixes whose `.trellis/...` strings are machine-typed citations. Prose is
# deliberately excluded -- see `rewrite_archived_task_refs`.
REF_CARRIER_SUFFIXES = (".json", ".jsonl")


def _task_ref_pattern(ref: str) -> re.Pattern[bytes]:
    """Match `ref` as a whole task path, not as a prefix of a longer name.

    `.trellis/tasks/06-15-tui` must not match inside
    `.trellis/tasks/06-15-tui-unit-tests`: those are two different tasks, and
    16 such name pairs exist in this repository.
    """
    return re.compile(re.escape(ref.encode("utf-8")) + rb"(?![\w.\-])")


def pre_archive_task_ref(archived_dir: Path, repo_root: Path) -> str | None:
    """Repo-relative path a task had BEFORE it was archived.

    `.trellis/tasks/archive/2026-09/09-13-t3-graph-runtime`
        -> `.trellis/tasks/09-13-t3-graph-runtime`
    """
    try:
        dest_rel = archived_dir.relative_to(repo_root).as_posix()
    except ValueError:
        return None

    prefix = f"{DIR_WORKFLOW}/{DIR_TASKS}/{DIR_ARCHIVE}/"
    if not dest_rel.startswith(prefix):
        return None

    parts = dest_rel[len(prefix):].split("/")
    if len(parts) < 2 or not parts[1]:
        return None
    return f"{DIR_WORKFLOW}/{DIR_TASKS}/{parts[1]}"


def rewrite_archived_task_refs(archived_dir: Path, repo_root: Path) -> dict[str, int]:
    """Repoint a just-archived task's own citations at its new location.

    `archive_task_dir` moves a directory and nothing else, but every citation a
    task makes to its OWN files is written at the pre-archive path
    (`implement.jsonl` / `check.jsonl` name `.trellis/tasks/<name>/prd.md`, and
    `task.json` names its own research notes). The move therefore breaks all of
    them at once, silently -- measured 2026-09-17 (#678), this is the dominant
    shape of the dangling `.trellis/tasks/**` corpus.

    Only JSON carriers are rewritten, because there a `.trellis/...` string is a
    machine-typed citation. A path inside `.md` prose may be a deliberate
    historical note ("this task moved from X"), and this repository already holds
    that a mention is not a reference, so prose is left for a human to repoint.

    Substitution is byte-level and boundary-aware: a citation of
    `.trellis/tasks/a-b` is a DIFFERENT task and is not touched, even though
    the string `.trellis/tasks/a` occurs inside it. Encoding and JSON escaping
    are untouched. Idempotent: the pre-archive prefix is gone once rewritten.

    Returns {repo-relative file: replacement count} for files that changed.
    """
    old_rel = pre_archive_task_ref(archived_dir, repo_root)
    if old_rel is None or not archived_dir.is_dir():
        return {}

    new_rel = archived_dir.relative_to(repo_root).as_posix()
    new_bytes = new_rel.encode("utf-8")
    pattern = _task_ref_pattern(old_rel)

    changed: dict[str, int] = {}
    for path in sorted(archived_dir.rglob("*")):
        if not path.is_file() or path.suffix not in REF_CARRIER_SUFFIXES:
            continue
        data = path.read_bytes()
        new_data, count = pattern.subn(new_bytes, data)
        if not count:
            continue
        path.write_bytes(new_data)
        changed[path.relative_to(repo_root).as_posix()] = count
    return changed


def find_external_task_refs(
    repo_root: Path,
    old_rel: str,
    skip_dir: Path | None = None,
) -> list[str]:
    """Task files OUTSIDE `skip_dir` that still cite `old_rel`.

    Reported, never rewritten: they belong to other tasks, and editing another
    task's provenance as a side effect of an archive would be a wider change than
    the command implies. The caller is expected to surface them.
    """
    if not old_rel:
        return []

    pattern = _task_ref_pattern(old_rel)
    suffixes = REF_CARRIER_SUFFIXES + (".md",)
    hits: list[str] = []
    for path in sorted(get_tasks_dir(repo_root).rglob("*")):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if skip_dir is not None and skip_dir in path.parents:
            continue
        try:
            if pattern.search(path.read_bytes()):
                hits.append(path.relative_to(repo_root).as_posix())
        except OSError:
            continue
    return hits


def archive_task_complete(
    task_dir_abs: Path,
    repo_root: Path | None = None
) -> dict[str, str]:
    """Complete archive workflow: archive directory.

    Args:
        task_dir_abs: Absolute path to task directory.
        repo_root: Repository root path. Defaults to auto-detected.

    Returns:
        Dict with archive result info.
    """
    if not task_dir_abs.is_dir():
        print(f"Error: task directory not found: {task_dir_abs}", file=sys.stderr)
        return {}

    archive_dest = archive_task_dir(task_dir_abs, repo_root)
    if archive_dest:
        return {"archived_to": str(archive_dest)}

    return {}


# =============================================================================
# Task Directory Resolution
# =============================================================================

def resolve_task_dir(target_dir: str, repo_root: Path) -> Path:
    """Resolve task directory to absolute path.

    Supports:
    - Absolute path: /path/to/task
    - Relative path: .trellis/tasks/01-31-my-task
    - Task name: my-task (uses find_task_by_name for lookup)

    Args:
        target_dir: Task directory specification.
        repo_root: Repository root path.

    Returns:
        Resolved absolute path.
    """
    if not target_dir:
        return Path()

    normalized = target_dir.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]

    # Absolute path
    if Path(target_dir).is_absolute():
        return Path(target_dir)

    # Relative path (contains path separator or starts with .trellis)
    if "/" in normalized or normalized.startswith(".trellis"):
        return repo_root / Path(normalized)

    # Task name - try to find in tasks directory
    tasks_dir = get_tasks_dir(repo_root)
    found = find_task_by_name(target_dir, tasks_dir)
    if found:
        return found

    # Fallback to treating as relative path
    return repo_root / Path(normalized)


# =============================================================================
# Lifecycle Hooks
# =============================================================================

def run_task_hooks(event: str, task_json_path: Path, repo_root: Path) -> None:
    """Run lifecycle hooks for a task event.

    Args:
        event: Event name (e.g. "after_create").
        task_json_path: Absolute path to the task's task.json.
        repo_root: Repository root for cwd and config lookup.
    """
    import os
    import subprocess

    from .config import get_hooks
    from .log import Colors, colored

    commands = get_hooks(event, repo_root)
    if not commands:
        return

    env = {**os.environ, "TASK_JSON_PATH": str(task_json_path)}

    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=repo_root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                print(
                    colored(f"[WARN] Hook failed ({event}): {cmd}", Colors.YELLOW),
                    file=sys.stderr,
                )
                if result.stderr.strip():
                    print(f"  {result.stderr.strip()}", file=sys.stderr)
        except Exception as e:
            print(
                colored(f"[WARN] Hook error ({event}): {cmd} — {e}", Colors.YELLOW),
                file=sys.stderr,
            )


# =============================================================================
# Main Entry (for testing)
# =============================================================================

if __name__ == "__main__":
    repo = get_repo_root()
    tasks = get_tasks_dir(repo)

    print(f"Tasks dir: {tasks}")
    print(f"is_safe_task_path('.trellis/tasks/test'): {is_safe_task_path('.trellis/tasks/test', repo)}")
    print(f"is_safe_task_path('../test'): {is_safe_task_path('../test', repo)}")
