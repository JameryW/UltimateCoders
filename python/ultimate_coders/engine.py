"""
Engine factory -- creates the unified engine interface.

Switches between local (PyO3 FFI) and remote (gRPC) at construction time.
Supports automatic fallback from gRPC to local mode when the remote server
is unavailable.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

try:
    from ultimate_coders._uc_core import PyEngine, PySearchQuery
except ImportError:
    PyEngine = None  # Rust extension not built yet
    PySearchQuery = None

logger = logging.getLogger(__name__)


class Engine:
    """Unified engine interface for UltimateCoders.

    Switches between local (PyO3 FFI, in-process) and remote (gRPC) mode.

    Usage:
        # Local mode (single machine)
        engine = Engine(mode="local")

        # Remote mode (cluster)
        engine = Engine(mode="grpc", grpc_endpoint="http://localhost:50051")

        # Health check
        status = engine.health()

        # Memory operations
        engine.write_memory("task", "decisions", "Use PostgreSQL", task_id="t1")
        entry = engine.read_memory("task", "decisions", task_id="t1")
        engine.delete_memory("task", "decisions", task_id="t1")

        # Search
        result = engine.search(query)

        # Index
        response = engine.index_repo("my-repo", "/path/to/local/clone")
    """

    def __init__(
        self,
        mode: str = "local",
        grpc_endpoint: str | None = None,
        fallback_mode: str = "none",
        on_fallback: Callable[[], None] | None = None,
        on_recovery: Callable[[], None] | None = None,
    ):
        """Initialize the Engine.

        Args:
            mode: "local" (in-process) or "grpc" (remote server).
            grpc_endpoint: Required if mode="grpc".
            fallback_mode: "none" (no fallback, default), or "auto"
                (if mode="grpc" and gRPC fails, auto-switch to local).
            on_fallback: Callback invoked when the engine falls back
                from gRPC to local mode.
            on_recovery: Callback invoked when the engine recovers
                back to gRPC mode after a fallback.
        """
        if PyEngine is None:
            raise ImportError(
                "Rust extension not built. Run `maturin develop` first."
            )
        self._mode = mode
        self._fallback_mode = fallback_mode
        self._fallback_active = False
        self._on_fallback = on_fallback
        self._on_recovery = on_recovery
        self._last_recovery_check: float = 0.0
        self._recovery_check_interval: float = 30.0  # seconds

        # Always create the local engine (needed for fallback)
        self._local_engine = PyEngine(mode="local", grpc_endpoint=None)

        if mode == "grpc":
            self._grpc_engine = PyEngine(mode=mode, grpc_endpoint=grpc_endpoint)
            self._engine = self._grpc_engine
        else:
            self._grpc_engine = None
            self._engine = self._local_engine

    @property
    def mode(self) -> str:
        """Current engine mode ('local' or 'grpc')."""
        return self._mode

    @property
    def fallback_active(self) -> bool:
        """Whether the engine is currently in fallback (local) mode.

        True only when mode="grpc", fallback_mode="auto", and the gRPC
        server has failed, causing the engine to fall back to local mode.
        """
        return self._fallback_active

    # ── Fallback helpers ─────────────────────────────────────────

    def _should_use_fallback(self) -> bool:
        """Whether fallback logic applies for the current call.

        True only when mode="grpc" and fallback_mode="auto".
        """
        return self._mode == "grpc" and self._fallback_mode == "auto"

    def _try_grpc_with_fallback(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Call a gRPC method with automatic fallback to local on failure.

        If the engine is currently in fallback mode, delegates to the local
        engine directly.  Otherwise tries the gRPC engine first; on
        ConnectionError or TimeoutError, activates fallback and retries
        on the local engine.

        Args:
            method_name: Name of the method to call on the engine.
            *args: Positional arguments for the method.
            **kwargs: Keyword arguments for the method.

        Returns:
            The result of the method call.
        """
        if not self._should_use_fallback():
            # No fallback configured — call the engine directly
            try:
                return getattr(self._engine, method_name)(*args, **kwargs)
            except AttributeError:
                raise AttributeError(
                    f"Engine has no method '{method_name}'"
                ) from None

        if self._fallback_active:
            # Already in fallback — check for recovery opportunity
            self._check_grpc_recovery()
            if self._fallback_active:
                try:
                    return getattr(self._local_engine, method_name)(*args, **kwargs)
                except AttributeError:
                    raise AttributeError(
                        f"Local engine has no method '{method_name}'"
                    ) from None

        # Try gRPC first
        try:
            result = getattr(self._grpc_engine, method_name)(*args, **kwargs)
            return result
        except AttributeError:
            raise AttributeError(
                f"gRPC engine has no method '{method_name}'"
            ) from None
        except (ConnectionError, TimeoutError, OSError) as exc:
            logger.warning(
                "gRPC %s failed (%s), falling back to local engine",
                method_name,
                exc,
            )
            self._activate_fallback()
            return getattr(self._local_engine, method_name)(*args, **kwargs)

    async def _try_grpc_with_fallback_async(
        self, method_name: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Async version of _try_grpc_with_fallback.

        Args:
            method_name: Name of the async method to call on the engine.
            *args: Positional arguments for the method.
            **kwargs: Keyword arguments for the method.

        Returns:
            The result of the async method call.
        """
        if not self._should_use_fallback():
            try:
                return await getattr(self._engine, method_name)(*args, **kwargs)
            except AttributeError:
                raise AttributeError(
                    f"Engine has no async method '{method_name}'"
                ) from None

        if self._fallback_active:
            self._check_grpc_recovery()
            if self._fallback_active:
                try:
                    return await getattr(self._local_engine, method_name)(*args, **kwargs)
                except AttributeError:
                    raise AttributeError(
                        f"Local engine has no async method '{method_name}'"
                    ) from None

        try:
            result = await getattr(self._grpc_engine, method_name)(*args, **kwargs)
            return result
        except AttributeError:
            raise AttributeError(
                f"gRPC engine has no async method '{method_name}'"
            ) from None
        except (ConnectionError, TimeoutError, OSError) as exc:
            logger.warning(
                "gRPC %s failed (%s), falling back to local engine",
                method_name,
                exc,
            )
            self._activate_fallback()
            return await getattr(self._local_engine, method_name)(*args, **kwargs)

    def _activate_fallback(self) -> None:
        """Switch the engine from gRPC to local fallback mode."""
        if self._fallback_active:
            return
        self._fallback_active = True
        self._engine = self._local_engine
        logger.info("Activated fallback to local engine")
        if self._on_fallback is not None:
            try:
                self._on_fallback()
            except Exception:
                logger.debug("on_fallback callback error", exc_info=True)

    def _check_grpc_recovery(self) -> None:
        """Try to recover back to gRPC mode.

        Periodically (every recovery_check_interval seconds) attempts a
        gRPC health() call.  If it succeeds, switches back to gRPC mode.
        """
        if not self._fallback_active or self._grpc_engine is None:
            return

        now = time.monotonic()
        if now - self._last_recovery_check < self._recovery_check_interval:
            return

        self._last_recovery_check = now
        try:
            self._grpc_engine.health()
            # Recovery successful
            self._fallback_active = False
            self._engine = self._grpc_engine
            logger.info("Recovered from fallback to gRPC engine")
            if self._on_recovery is not None:
                try:
                    self._on_recovery()
                except Exception:
                    logger.debug("on_recovery callback error", exc_info=True)
        except (ConnectionError, TimeoutError, OSError):
            logger.debug("gRPC recovery check failed, staying in fallback")

    # ── Public API (with fallback wrapping) ──────────────────────

    def health(self) -> object:
        """Check engine health. Returns a HealthStatus object with
        .status, .version, .uptime_seconds, and .components attributes."""
        return self._try_grpc_with_fallback("health")

    def search(self, query) -> object:
        """Search across indexed repositories.

        Args:
            query: A SearchQuery object (builder), PySearchQuery (Rust type),
                   or dict with search parameters.

        Returns:
            SearchResult with matching items.
        """
        py_query = self._convert_search_query(query)
        return self._try_grpc_with_fallback("search", py_query)

    def _convert_search_query(self, query: Any) -> Any:
        """Convert a SearchQuery builder or dict to a PySearchQuery for the Rust engine.

        Args:
            query: SearchQuery (Python builder), PySearchQuery (Rust type), or dict.

        Returns:
            PySearchQuery object suitable for the Rust engine.
        """
        # Already a PySearchQuery -- pass through
        if PySearchQuery is not None and isinstance(query, PySearchQuery):
            return query

        # Python SearchQuery builder -- convert to PySearchQuery
        if hasattr(query, "to_dict"):
            data = query.to_dict()
            if PySearchQuery is not None:
                return PySearchQuery(
                    query=data["query"],
                    modes=data.get("modes", []),
                    repo_ids=data.get("repo_ids", []),
                    languages=data.get("languages", []),
                    path_patterns=data.get("path_patterns", []),
                    max_results=data.get("max_results", 10),
                )

        # Dict -- convert to PySearchQuery
        if isinstance(query, dict):
            if PySearchQuery is not None:
                return PySearchQuery(
                    query=query["query"],
                    modes=query.get("modes", []),
                    repo_ids=query.get("repo_ids", []),
                    languages=query.get("languages", []),
                    path_patterns=query.get("path_patterns", []),
                    max_results=query.get("max_results", 10),
                )

        # Fallback: pass through and let Rust handle the error
        return query

    def index_repo(
        self,
        repo_id: str,
        local_path: str,
        remote_url: str | None = None,
        default_branch: str = "main",
        force_full: bool = False,
    ) -> object:
        """Index a repository for search.

        Args:
            repo_id: Repository identifier.
            local_path: Local path to the repository clone.
            remote_url: Git remote URL (optional).
            default_branch: Default branch name (default: "main").
            force_full: Force full reindex (default: False).

        Returns:
            IndexResponse with indexing statistics.
        """
        return self._try_grpc_with_fallback(
            "index_repo",
            repo_id, local_path, remote_url, default_branch, force_full,
        )

    def get_index_state(self, repo_id: str) -> object:
        """Get the current index state for a repository.

        Args:
            repo_id: Repository identifier.

        Returns:
            RepoIndexState with index status information.
        """
        return self._try_grpc_with_fallback("get_index_state", repo_id)

    def remove_index(self, repo_id: str) -> None:
        """Remove a repository's index.

        Args:
            repo_id: Repository identifier.
        """
        return self._try_grpc_with_fallback("remove_index", repo_id)

    def read_memory(
        self,
        key_scope: str,
        key: str,
        task_id: str | None = None,
        project_id: str | None = None,
        include_semantic: bool = False,
    ) -> object | None:
        """Read a memory entry.

        Args:
            key_scope: "task", "project", or "global"
            key: The memory key name.
            task_id: Task ID (required if key_scope="task").
            project_id: Project ID (required if key_scope="project").
            include_semantic: Also search long-term memory semantically.

        Returns:
            MemoryEntry or None if not found.
        """
        return self._try_grpc_with_fallback(
            "read_memory",
            key_scope, key, task_id, project_id, include_semantic,
        )

    def write_memory(
        self,
        key_scope: str,
        key: str,
        content: str,
        content_type: str = "text",
        source_agent: str = "python",
        importance: float = 0.5,
        tags: list | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        language: str | None = None,
        file_path: str | None = None,
        uri: str | None = None,
        description: str | None = None,
    ) -> object:
        """Write a memory entry.

        Args:
            key_scope: "task", "project", or "global"
            key: The memory key name.
            content: The content to store.
            content_type: "text", "structured", "code", "diff", or "reference".
            source_agent: Agent that created this memory.
            importance: Importance score (0.0-1.0).
            tags: Tags for categorization.
            task_id: Task ID (required if key_scope="task").
            project_id: Project ID (required if key_scope="project").
            language: Language for content_type="code".
            file_path: File path for content_type="diff".
            uri: URI for content_type="reference".
            description: Description for content_type="reference".

        Returns:
            MemoryEntry with the written data.
        """
        return self._try_grpc_with_fallback(
            "write_memory",
            key_scope, key, content, content_type, source_agent,
            importance, tags, task_id, project_id,
            language, file_path, uri, description,
        )

    def delete_memory(
        self,
        key_scope: str,
        key: str,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Delete a memory entry.

        Args:
            key_scope: "task", "project", or "global"
            key: The memory key name.
            task_id: Task ID (required if key_scope="task").
            project_id: Project ID (required if key_scope="project").
        """
        return self._try_grpc_with_fallback(
            "delete_memory", key_scope, key, task_id, project_id,
        )

    def search_memory(
        self,
        query: str,
        scope_type: str = "all",
        project_id: str | None = None,
        max_results: int = 20,
        min_score: float = 0.5,
    ) -> list:
        """Search long-term memory semantically.

        Args:
            query: Search query text.
            scope_type: "project", "global", or "all".
            project_id: Project ID (required if scope_type="project").
            max_results: Maximum number of results.
            min_score: Minimum similarity score (0.0-1.0).

        Returns:
            List of MemorySearchResult objects.
        """
        return self._try_grpc_with_fallback(
            "search_memory",
            query, scope_type, project_id, max_results, min_score,
        )

    def watch_task(
        self,
        task_id: str,
        max_events: int = 50,
        timeout_secs: float = 10.0,
    ) -> list:
        """Watch a task for events (gRPC mode only).

        Collects events from the server-streaming WatchTask RPC,
        returning up to max_events within timeout_secs.
        Use in a polling loop for continuous monitoring.

        Args:
            task_id: The task to watch (empty string watches all tasks).
            max_events: Maximum events to collect (default: 50).
            timeout_secs: Timeout in seconds (default: 10.0).

        Returns:
            List of AgentEvent objects with event_type, task_id,
            subtask_id, data, and timestamp attributes.
        """
        return self._try_grpc_with_fallback(
            "watch_task", task_id, max_events, timeout_secs,
        )

    # ── Batch / List / Stream ──────────────────────────────────

    def batch_write_memory(self, requests: list[dict]) -> list:
        """Write multiple memory entries in a single call.

        Args:
            requests: List of dicts, each with keys:
                key_scope (str), key (str), content (str), and optionally
                content_type, source_agent, importance, tags, task_id,
                project_id, language, file_path, uri, description.

        Returns:
            List of MemoryEntry objects.
        """
        return self._try_grpc_with_fallback("batch_write_memory", requests)

    def list_repos(self) -> list:
        """List all indexed repositories.

        Returns:
            List of RepoIndexState objects.
        """
        return self._try_grpc_with_fallback("list_repos")

    def search_stream(self, query) -> list:
        """Stream search results, collected into a list.

        Args:
            query: A SearchQuery object (builder), PySearchQuery, or dict.

        Returns:
            List of SearchResultItem objects.
        """
        py_query = self._convert_search_query(query)
        return self._try_grpc_with_fallback("search_stream", py_query)

    # ── Task Orchestration ─────────────────────────────────────

    def submit_task(self, description: str, project_id: str | None = None) -> object:
        """Submit a new task for orchestration.

        Args:
            description: Task description.
            project_id: Project ID (default: empty string).

        Returns:
            Task object with id, description, status, etc.
        """
        return self._try_grpc_with_fallback(
            "submit_task", description, project_id or ""
        )

    def get_task(self, task_id: str) -> object:
        """Get a task by ID.

        Args:
            task_id: The task ID.

        Returns:
            Task object.
        """
        return self._try_grpc_with_fallback("get_task", task_id)

    def list_tasks(self) -> list:
        """List all tasks.

        Returns:
            List of Task objects.
        """
        return self._try_grpc_with_fallback("list_tasks")

    def pause_task(self, task_id: str) -> object:
        """Pause a running task.

        Args:
            task_id: The task ID.

        Returns:
            Updated Task object.
        """
        return self._try_grpc_with_fallback("pause_task", task_id)

    def resume_task(self, task_id: str) -> object:
        """Resume a paused task.

        Args:
            task_id: The task ID.

        Returns:
            Updated Task object.
        """
        return self._try_grpc_with_fallback("resume_task", task_id)

    # ── Async methods ──────────────────────────────────────────

    async def health_async(self) -> object:
        """Async version of health(). Returns full HealthStatus object.

        Usage:
            status = await engine.health_async()
        """
        return await self._try_grpc_with_fallback_async("health_async")

    async def search_async(self, query) -> object:
        """Async version of search().

        Args:
            query: A SearchQuery object (builder), PySearchQuery (Rust type),
                   or dict with search parameters.

        Usage:
            result = await engine.search_async(query)
        """
        py_query = self._convert_search_query(query)
        return await self._try_grpc_with_fallback_async("search_async", py_query)

    async def index_repo_async(
        self,
        repo_id: str,
        local_path: str,
        remote_url: str | None = None,
        default_branch: str = "main",
        force_full: bool = False,
    ) -> object:
        """Async version of index_repo().

        Args:
            repo_id: Repository identifier.
            local_path: Local path to the repository clone.
            remote_url: Git remote URL (optional).
            default_branch: Default branch name (default: "main").
            force_full: Force full reindex (default: False).

        Usage:
            response = await engine.index_repo_async("my-repo", "/path/to/repo")
        """
        return await self._try_grpc_with_fallback_async(
            "index_repo_async",
            repo_id, local_path, remote_url, default_branch, force_full,
        )

    async def read_memory_async(
        self,
        key_scope: str,
        key: str,
        task_id: str | None = None,
        project_id: str | None = None,
        include_semantic: bool = False,
    ) -> object | None:
        """Async version of read_memory().

        Args:
            key_scope: "task", "project", or "global"
            key: The memory key name.
            task_id: Task ID (required if key_scope="task").
            project_id: Project ID (required if key_scope="project").
            include_semantic: Also search long-term memory semantically.

        Usage:
            entry = await engine.read_memory_async("task", "decisions", task_id="t1")
        """
        return await self._try_grpc_with_fallback_async(
            "read_memory_async",
            key_scope, key, task_id, project_id, include_semantic,
        )

    async def write_memory_async(
        self,
        key_scope: str,
        key: str,
        content: str,
        content_type: str = "text",
        source_agent: str = "python",
        importance: float = 0.5,
        tags: list | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        language: str | None = None,
        file_path: str | None = None,
        uri: str | None = None,
        description: str | None = None,
    ) -> object:
        """Async version of write_memory().

        Args:
            key_scope: "task", "project", or "global"
            key: The memory key name.
            content: The content to store.
            content_type: "text", "structured", "code", "diff", or "reference".
            source_agent: Agent that created this memory.
            importance: Importance score (0.0-1.0).
            tags: Tags for categorization.
            task_id: Task ID (required if key_scope="task").
            project_id: Project ID (required if key_scope="project").
            language: Language for content_type="code".
            file_path: File path for content_type="diff".
            uri: URI for content_type="reference".
            description: Description for content_type="reference".

        Usage:
            entry = await engine.write_memory_async(
                "task", "decisions", "Use PostgreSQL", task_id="t1"
            )
        """
        return await self._try_grpc_with_fallback_async(
            "write_memory_async",
            key_scope, key, content, content_type, source_agent,
            importance, tags, task_id, project_id,
            language, file_path, uri, description,
        )

    async def delete_memory_async(
        self,
        key_scope: str,
        key: str,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Async version of delete_memory().

        Args:
            key_scope: "task", "project", or "global"
            key: The memory key name.
            task_id: Task ID (required if key_scope="task").
            project_id: Project ID (required if key_scope="project").

        Usage:
            await engine.delete_memory_async("task", "decisions", task_id="t1")
        """
        await self._try_grpc_with_fallback_async(
            "delete_memory_async", key_scope, key, task_id, project_id,
        )

    async def search_memory_async(
        self,
        query: str,
        scope_type: str = "all",
        project_id: str | None = None,
        max_results: int = 20,
        min_score: float = 0.5,
    ) -> list:
        """Async version of search_memory().

        Args:
            query: Search query text.
            scope_type: "project", "global", or "all".
            project_id: Project ID (required if scope_type="project").
            max_results: Maximum number of results.
            min_score: Minimum similarity score (0.0-1.0).

        Usage:
            results = await engine.search_memory_async("database patterns")
        """
        return await self._try_grpc_with_fallback_async(
            "search_memory_async",
            query, scope_type, project_id, max_results, min_score,
        )

    async def get_index_state_async(self, repo_id: str) -> object:
        """Async version of get_index_state().

        Args:
            repo_id: Repository identifier.

        Usage:
            state = await engine.get_index_state_async("my-repo")
        """
        return await self._try_grpc_with_fallback_async(
            "get_index_state_async", repo_id,
        )

    async def get_detailed_index_state_async(self, repo_id: str) -> dict:
        """Async version of get_detailed_index_state().

        Args:
            repo_id: The repository identifier.
        """
        return await self._try_grpc_with_fallback_async(
            "get_detailed_index_state_async", repo_id,
        )

    async def remove_index_async(self, repo_id: str) -> None:
        """Async version of remove_index().

        Args:
            repo_id: Repository identifier.

        Usage:
            await engine.remove_index_async("my-repo")
        """
        await self._try_grpc_with_fallback_async(
            "remove_index_async", repo_id,
        )

    async def watch_task_async(
        self,
        task_id: str,
        max_events: int = 50,
        timeout_secs: float = 10.0,
    ) -> list:
        """Async version of watch_task() (gRPC mode only).

        Collects events from the server-streaming WatchTask RPC,
        returning up to max_events within timeout_secs.

        Args:
            task_id: The task to watch (empty string watches all tasks).
            max_events: Maximum events to collect (default: 50).
            timeout_secs: Timeout in seconds (default: 10.0).

        Returns:
            List of AgentEvent objects.

        Usage:
            events = await engine.watch_task_async("task-123", max_events=20)
        """
        return await self._try_grpc_with_fallback_async(
            "watch_task_async", task_id, max_events, timeout_secs,
        )

    # ── Async Batch / List / Stream ────────────────────────────

    async def batch_write_memory_async(self, requests: list[dict]) -> list:
        """Async version of batch_write_memory().

        Args:
            requests: List of dicts (see batch_write_memory).

        Returns:
            List of MemoryEntry objects.
        """
        return await self._try_grpc_with_fallback_async(
            "batch_write_memory_async", requests
        )

    async def list_repos_async(self) -> list:
        """Async version of list_repos().

        Returns:
            List of RepoIndexState objects.
        """
        return await self._try_grpc_with_fallback_async("list_repos_async")

    async def search_stream_async(self, query) -> list:
        """Async version of search_stream().

        Args:
            query: A SearchQuery object (builder), PySearchQuery, or dict.

        Returns:
            List of SearchResultItem objects.
        """
        py_query = self._convert_search_query(query)
        return await self._try_grpc_with_fallback_async(
            "search_stream_async", py_query
        )

    # ── Async Task Orchestration ───────────────────────────────

    async def submit_task_async(
        self, description: str, project_id: str | None = None
    ) -> object:
        """Async version of submit_task().

        Args:
            description: Task description.
            project_id: Project ID (default: empty string).
        """
        return await self._try_grpc_with_fallback_async(
            "submit_task_async", description, project_id or ""
        )

    async def get_task_async(self, task_id: str) -> object:
        """Async version of get_task().

        Args:
            task_id: The task ID.
        """
        return await self._try_grpc_with_fallback_async(
            "get_task_async", task_id
        )

    async def list_tasks_async(self) -> list:
        """Async version of list_tasks()."""
        return await self._try_grpc_with_fallback_async("list_tasks_async")

    async def pause_task_async(self, task_id: str) -> object:
        """Async version of pause_task().

        Args:
            task_id: The task ID.
        """
        return await self._try_grpc_with_fallback_async(
            "pause_task_async", task_id
        )

    async def resume_task_async(self, task_id: str) -> object:
        """Async version of resume_task().

        Args:
            task_id: The task ID.
        """
        return await self._try_grpc_with_fallback_async(
            "resume_task_async", task_id
        )


def create_engine(
    mode: str = "local",
    grpc_endpoint: str | None = None,
    fallback_mode: str = "none",
    on_fallback: Callable[[], None] | None = None,
    on_recovery: Callable[[], None] | None = None,
) -> Engine:
    """Factory function to create an Engine instance.

    Args:
        mode: "local" (in-process) or "grpc" (remote server)
        grpc_endpoint: Required if mode="grpc"
        fallback_mode: "none" (default) or "auto" (fallback to local on gRPC failure)
        on_fallback: Callback invoked when fallback to local mode occurs
        on_recovery: Callback invoked when recovery to gRPC mode occurs

    Returns:
        Engine instance
    """
    return Engine(
        mode=mode,
        grpc_endpoint=grpc_endpoint,
        fallback_mode=fallback_mode,
        on_fallback=on_fallback,
        on_recovery=on_recovery,
    )
