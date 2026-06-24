"""RepoConfig — YAML-based multi-repo configuration + auto-discovery.

Loads repos.yaml, scans scan_dirs for git repos, and provides
a watchdog-based hot-reload mechanism.

Config file priority:
  CLI argument > UC_REPOS_CONFIG env var > ./uc.repos.yaml (CWD)
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class RepoEntry:
    """A single repository declaration from repos.yaml."""

    repo_id: str
    local_path: str
    remote_url: str = ""
    default_branch: str = "main"
    tags: list[str] = field(default_factory=list)


@dataclass
class RepoConfig:
    """Full repos.yaml configuration."""

    repos: list[RepoEntry] = field(default_factory=list)
    scan_dirs: list[str] = field(default_factory=list)
    scan_depth: int = 3


def load_repos_config(path: str | Path | None = None) -> RepoConfig:
    """Load repos.yaml from the given path or auto-discover it.

    Search order:
      1. Explicit path argument
      2. UC_REPOS_CONFIG environment variable
      3. ./uc.repos.yaml in current working directory

    Returns RepoConfig with empty defaults if no file found.
    """
    resolved = _resolve_config_path(path)
    if resolved is None:
        logger.debug("No repos config file found; using defaults")
        return RepoConfig()

    return _parse_repos_yaml(resolved)


def _resolve_config_path(path: str | Path | None = None) -> Path | None:
    """Resolve the config file path following priority rules."""
    if path is not None:
        p = Path(path)
        if p.exists():
            return p
        logger.warning("repos config path %s does not exist", p)
        return None

    env_path = os.environ.get("UC_REPOS_CONFIG")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
        logger.warning("UC_REPOS_CONFIG path %s does not exist", env_path)
        return None

    for name in ("uc.repos.yaml", "uc.repos.yml"):
        p = Path(name)
        if p.exists():
            return p

    return None


def _parse_repos_yaml(path: Path) -> RepoConfig:
    """Parse a repos.yaml file into RepoConfig."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.error("PyYAML not installed; cannot parse repos.yaml")
        return RepoConfig()

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    repos: list[RepoEntry] = []
    for r in data.get("repos", []):
        repos.append(RepoEntry(
            repo_id=r.get("repo_id", ""),
            local_path=r.get("local_path", ""),
            remote_url=r.get("remote_url", ""),
            default_branch=r.get("default_branch", "main"),
            tags=r.get("tags", []),
        ))

    # Validate: repo_id and local_path are required
    repos = [r for r in repos if r.repo_id and r.local_path]

    scan_dirs = data.get("scan_dirs", [])
    scan_depth = data.get("scan_depth", 3)

    return RepoConfig(repos=repos, scan_dirs=scan_dirs, scan_depth=scan_depth)


class RepoScanner:
    """Scan directories for git repositories."""

    def __init__(self, engine: object | None = None) -> None:
        """Initialize scanner.

        Args:
            engine: Engine instance with index_repo() method.
                    If None, scanning only discovers without indexing.
        """
        self._engine = engine

    def discover(self, scan_dirs: list[str], scan_depth: int = 3) -> list[RepoEntry]:
        """Scan directories for git repos and return RepoEntry list.

        Args:
            scan_dirs: Directories to scan.
            scan_depth: Max directory depth to descend.

        Returns:
            List of discovered RepoEntry objects.
        """
        found: list[RepoEntry] = []
        for dir_path in scan_dirs:
            found.extend(self._scan_dir(dir_path, scan_depth))
        return found

    def discover_and_index(
        self,
        config: RepoConfig,
        indexed_repo_ids: set[str] | None = None,
    ) -> list[RepoEntry]:
        """Discover repos from scan_dirs and index new ones.

        Args:
            config: RepoConfig with scan_dirs and scan_depth.
            indexed_repo_ids: Set of already-indexed repo IDs to skip.

        Returns:
            List of newly discovered RepoEntry objects.
        """
        if indexed_repo_ids is None:
            indexed_repo_ids = set()

        discovered = self.discover(config.scan_dirs, config.scan_depth)
        new_repos: list[RepoEntry] = []

        for entry in discovered:
            if entry.repo_id in indexed_repo_ids:
                continue
            new_repos.append(entry)
            if self._engine is not None:
                try:
                    self._engine.index_repo(
                        entry.repo_id,
                        entry.local_path,
                        entry.remote_url or None,
                        entry.default_branch,
                    )
                    indexed_repo_ids.add(entry.repo_id)
                    logger.info("Auto-discovered and indexed repo: %s", entry.repo_id)
                except Exception:
                    logger.warning(
                        "Failed to index discovered repo %s",
                        entry.repo_id, exc_info=True,
                    )

        return new_repos

    def _scan_dir(self, dir_path: str, max_depth: int) -> list[RepoEntry]:
        """Recursively scan a directory for git repos."""
        results: list[RepoEntry] = []
        root = Path(dir_path)

        if not root.is_dir():
            logger.debug("scan_dirs entry does not exist: %s", dir_path)
            return results

        self._walk(root, max_depth, results)
        return results

    def _walk(self, dir_path: Path, depth: int, results: list[RepoEntry]) -> None:
        """Walk directory tree looking for .git directories."""
        if depth < 0:
            return

        # Skip hidden directories and common non-project dirs
        if dir_path.name.startswith(".") or dir_path.name in (
            "node_modules", "__pycache__", ".tox", ".venv", "venv",
        ):
            return

        git_dir = dir_path / ".git"
        if git_dir.is_dir():
            repo_id = self._derive_repo_id(dir_path)
            remote_url = self._get_remote_url(dir_path)
            branch = self._get_default_branch(dir_path)
            results.append(RepoEntry(
                repo_id=repo_id,
                local_path=str(dir_path),
                remote_url=remote_url,
                default_branch=branch,
            ))
            # Don't recurse into git repos — they won't contain other git repos
            return

        try:
            for child in dir_path.iterdir():
                if child.is_dir():
                    self._walk(child, depth - 1, results)
        except PermissionError:
            logger.debug("Permission denied scanning: %s", dir_path)

    @staticmethod
    def _derive_repo_id(path: Path) -> str:
        """Derive a repo_id from directory name or git remote."""
        remote = RepoScanner._get_remote_url(path)
        if remote:
            # Extract last segment of URL, strip .git
            # e.g. https://github.com/org/repo.git -> repo
            name = remote.rstrip("/").rsplit("/", 1)[-1]
            if name.endswith(".git"):
                name = name[:-4]
            if name:
                return name
        # Fallback: directory name
        return path.name

    @staticmethod
    def _get_remote_url(path: Path) -> str:
        """Get the git remote origin URL for a repo."""
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return ""

    @staticmethod
    def _get_default_branch(path: Path) -> str:
        """Get the default branch name for a git repo."""
        try:
            result = subprocess.run(
                ["git", "symbolic-ref", "--short", "HEAD"],
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return "main"


class RepoConfigWatcher:
    """Watch repos.yaml for changes and trigger reload via callback.

    Uses watchdog library for filesystem events. Falls back gracefully
    if watchdog is not available.
    """

    def __init__(
        self,
        config_path: str | Path,
        on_change: Callable[[RepoConfig], None],
        poll_interval: float = 2.0,
    ) -> None:
        """Initialize watcher.

        Args:
            config_path: Path to repos.yaml to watch.
            on_change: Callback invoked with new RepoConfig when file changes.
            poll_interval: Fallback polling interval in seconds.
        """
        self._config_path = Path(config_path)
        self._on_change = on_change
        self._poll_interval = poll_interval
        self._observer: object | None = None
        self._running = False
        self._last_mtime: float = 0.0

    def start(self) -> None:
        """Start watching the config file."""
        self._running = True
        try:
            from watchdog.events import FileSystemEventHandler  # type: ignore[import-untyped]
            from watchdog.observers import Observer  # type: ignore[import-untyped]

            parent_dir = str(self._config_path.parent)
            filename = self._config_path.name

            class Handler(FileSystemEventHandler):  # type: ignore[misc]
                def __init__(self, watcher: RepoConfigWatcher) -> None:  # noqa: N805
                    self._watcher = watcher  # type: ignore[attr-defined]

                def on_modified(self, event: object) -> None:  # noqa: N805
                    from watchdog.events import FileModifiedEvent  # type: ignore[import-untyped]
                    if isinstance(event, FileModifiedEvent) and event.src_path.endswith(filename):
                        self._watcher._reload()  # type: ignore[attr-defined]

            self._observer = Observer()
            self._observer.schedule(Handler(self), parent_dir, recursive=False)  # type: ignore[union-attr]
            self._observer.start()  # type: ignore[union-attr]
            logger.info("watchdog watching %s", self._config_path)
        except ImportError:
            logger.warning("watchdog not installed; config hot-reload disabled")

    def stop(self) -> None:
        """Stop watching."""
        self._running = False
        if self._observer is not None:
            self._observer.stop()  # type: ignore[union-attr]
            self._observer.join(timeout=5)  # type: ignore[union-attr]
            self._observer = None

    def _reload(self) -> None:
        """Reload config and invoke callback."""
        try:
            config = _parse_repos_yaml(self._config_path)
            self._on_change(config)
            logger.info("Reloaded repos config from %s", self._config_path)
        except Exception:
            logger.warning("Failed to reload repos config", exc_info=True)
