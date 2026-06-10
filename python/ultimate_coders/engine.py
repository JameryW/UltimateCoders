"""
Engine factory -- creates the unified engine interface.

Switches between local (PyO3 FFI) and remote (gRPC) at construction time.
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from ultimate_coders._uc_core import PyEngine, PySearchQuery
except ImportError:
    PyEngine = None  # Rust extension not built yet
    PySearchQuery = None


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
        grpc_endpoint: Optional[str] = None,
    ):
        if PyEngine is None:
            raise ImportError(
                "Rust extension not built. Run `maturin develop` first."
            )
        self._mode = mode
        self._engine = PyEngine(mode=mode, grpc_endpoint=grpc_endpoint)

    @property
    def mode(self) -> str:
        """Current engine mode ('local' or 'grpc')."""
        return self._mode

    def health(self) -> str:
        """Check engine health. Returns status string ('ok', 'degraded', 'error')."""
        return self._engine.health()

    def search(self, query) -> object:
        """Search across indexed repositories.

        Args:
            query: A SearchQuery object (builder), PySearchQuery (Rust type),
                   or dict with search parameters.

        Returns:
            SearchResult with matching items.
        """
        py_query = self._convert_search_query(query)
        return self._engine.search(py_query)

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
                    max_results=query.get("max_results", 10),
                )

        # Fallback: pass through and let Rust handle the error
        return query

    def index_repo(
        self,
        repo_id: str,
        local_path: str,
        remote_url: Optional[str] = None,
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
        return self._engine.index_repo(
            repo_id, local_path, remote_url, default_branch, force_full
        )

    def get_index_state(self, repo_id: str) -> object:
        """Get the current index state for a repository.

        Args:
            repo_id: Repository identifier.

        Returns:
            RepoIndexState with index status information.
        """
        return self._engine.get_index_state(repo_id)

    def remove_index(self, repo_id: str) -> None:
        """Remove a repository's index.

        Args:
            repo_id: Repository identifier.
        """
        self._engine.remove_index(repo_id)

    def read_memory(
        self,
        key_scope: str,
        key: str,
        task_id: Optional[str] = None,
        project_id: Optional[str] = None,
        include_semantic: bool = False,
    ) -> Optional[object]:
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
        return self._engine.read_memory(
            key_scope, key, task_id, project_id, include_semantic
        )

    def write_memory(
        self,
        key_scope: str,
        key: str,
        content: str,
        content_type: str = "text",
        source_agent: str = "python",
        importance: float = 0.5,
        tags: Optional[list] = None,
        task_id: Optional[str] = None,
        project_id: Optional[str] = None,
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

        Returns:
            MemoryEntry with the written data.
        """
        return self._engine.write_memory(
            key_scope, key, content, content_type, source_agent,
            importance, tags, task_id, project_id,
        )

    def delete_memory(
        self,
        key_scope: str,
        key: str,
        task_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> None:
        """Delete a memory entry.

        Args:
            key_scope: "task", "project", or "global"
            key: The memory key name.
            task_id: Task ID (required if key_scope="task").
            project_id: Project ID (required if key_scope="project").
        """
        self._engine.delete_memory(key_scope, key, task_id, project_id)

    def search_memory(
        self,
        query: str,
        scope_type: str = "all",
        project_id: Optional[str] = None,
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
        return self._engine.search_memory(
            query, scope_type, project_id, max_results, min_score
        )


def create_engine(
    mode: str = "local",
    grpc_endpoint: Optional[str] = None,
) -> Engine:
    """Factory function to create an Engine instance.

    Args:
        mode: "local" (in-process) or "grpc" (remote server)
        grpc_endpoint: Required if mode="grpc"

    Returns:
        Engine instance
    """
    return Engine(mode=mode, grpc_endpoint=grpc_endpoint)