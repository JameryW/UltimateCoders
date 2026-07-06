"""Memory wrappers — Pythonic interface to the layered memory system.

Short-term memory (task-scoped, TiKV-backed): fast KV access for
volatile task context such as code diffs, decisions, and progress.

Long-term memory (project-scoped, Qdrant-backed): persistent knowledge
with semantic search for architecture understanding, decision history,
and pattern accumulation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MemoryKey:
    """Represents a key in the layered memory system.

    The scope determines which storage layer is used:
    - "task": Short-term memory (TiKV), requires task_id
    - "project": Long-term memory (Qdrant), requires project_id
    - "global": Long-term memory (Qdrant), no scope qualifier
    """
    scope: str
    key: str
    task_id: str | None = None
    project_id: str | None = None

    def validate(self) -> None:
        """Validate that required scope qualifiers are present."""
        if self.scope == "task" and not self.task_id:
            raise ValueError("task_id is required for task-scoped memory")
        if self.scope == "project" and not self.project_id:
            raise ValueError("project_id is required for project-scoped memory")
        if self.scope not in ("task", "project", "global"):
            raise ValueError(
                f"Invalid scope: {self.scope!r}. "
                "Must be 'task', 'project', or 'global'."
            )


@dataclass
class MemoryEntry:
    """A memory entry with content and metadata.

    Wraps either a PyMemoryEntry from the Rust extension or a dict.
    """
    id: str = ""
    key: MemoryKey = field(default_factory=lambda: MemoryKey(scope="global", key=""))
    content: str = ""
    content_type: str = "text"
    source_agent: str = ""
    importance: float = 0.5
    tags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_rust(cls, raw: Any) -> MemoryEntry:
        """Create a MemoryEntry from a PyMemoryEntry (Rust extension).

        PyMemoryEntry exposes FLAT fields (not the old content-enum/key-enum
        shape): ``key_scope``, ``key``, ``task_id``, ``project_id``,
        ``content_type``, ``content``, ``source_agent``, ``importance``,
        ``tags``, ``created_at``/``updated_at`` (i64 epoch millis).
        """
        scope = getattr(raw, "key_scope", "global") or "global"
        mem_key = MemoryKey(
            scope=scope,
            key=getattr(raw, "key", "") or "",
            task_id=getattr(raw, "task_id", None),
            project_id=getattr(raw, "project_id", None),
        )

        # PyMemoryEntry stores created_at/updated_at as i64 epoch millis;
        # MemoryEntry holds datetime|None.
        def _to_dt(millis: Any) -> datetime | None:
            if not isinstance(millis, (int, float)) or millis <= 0:
                return None
            try:
                return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                return None

        return cls(
            id=getattr(raw, "id", "") or "",
            key=mem_key,
            content=getattr(raw, "content", "") or "",
            content_type=getattr(raw, "content_type", "text") or "text",
            source_agent=getattr(raw, "source_agent", "") or "",
            importance=float(getattr(raw, "importance", 0.5) or 0.5),
            tags=list(getattr(raw, "tags", []) or []),
            created_at=_to_dt(getattr(raw, "created_at", None)),
            updated_at=_to_dt(getattr(raw, "updated_at", None)),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        """Create a MemoryEntry from a dict.

        Args:
            data: Dict with memory entry fields.

        Returns:
            MemoryEntry with extracted fields.
        """
        key_data = data.get("key", {})
        if isinstance(key_data, dict):
            mem_key = MemoryKey(
                scope=key_data.get("scope", "global"),
                key=key_data.get("key", ""),
                task_id=key_data.get("task_id"),
                project_id=key_data.get("project_id"),
            )
        else:
            mem_key = MemoryKey(scope="global", key=str(key_data))

        return cls(
            id=data.get("id", ""),
            key=mem_key,
            content=data.get("content", ""),
            content_type=data.get("content_type", "text"),
            source_agent=data.get("source_agent", ""),
            importance=data.get("importance", 0.5),
            tags=data.get("tags", []),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


class ShortTermMemory:
    """Short-term memory (task-level context).

    Delegates to the Engine for storage operations. Uses the
    "task" key scope which maps to TiKV-backed storage.

    Usage:
        stm = ShortTermMemory(engine)
        stm.write("decisions", "Use PostgreSQL", task_id="t1")
        entry = stm.read("decisions", task_id="t1")
        stm.delete("decisions", task_id="t1")
    """

    def __init__(self, engine: Any):
        """Initialize ShortTermMemory.

        Args:
            engine: Engine instance with read_memory/write_memory/delete_memory.
        """
        self.engine = engine

    def read(
        self,
        key: str,
        task_id: str,
        project_id: str | None = None,
    ) -> MemoryEntry | None:
        """Read a value from short-term memory.

        Args:
            key: The memory key name.
            task_id: Task ID for scoping.
            project_id: Optional project ID.

        Returns:
            MemoryEntry or None if not found.
        """
        raw = self.engine.read_memory(
            key_scope="task",
            key=key,
            task_id=task_id,
            project_id=project_id,
        )
        if raw is None:
            return None
        return self._to_entry(raw)

    def write(
        self,
        key: str,
        content: str,
        task_id: str,
        content_type: str = "text",
        source_agent: str = "python",
        importance: float = 0.5,
        tags: list[str] | None = None,
        project_id: str | None = None,
    ) -> MemoryEntry:
        """Write a value to short-term memory.

        Args:
            key: The memory key name.
            content: Content to store.
            task_id: Task ID for scoping.
            content_type: Content type ("text", "structured", "code", "diff", "reference").
            source_agent: Agent that created this memory.
            importance: Importance score (0.0-1.0).
            tags: Tags for categorization.
            project_id: Optional project ID.

        Returns:
            MemoryEntry with the written data.
        """
        raw = self.engine.write_memory(
            key_scope="task",
            key=key,
            content=content,
            content_type=content_type,
            source_agent=source_agent,
            importance=importance,
            tags=tags,
            task_id=task_id,
            project_id=project_id,
        )
        return self._to_entry(raw)

    def delete(
        self,
        key: str,
        task_id: str,
        project_id: str | None = None,
    ) -> None:
        """Delete a value from short-term memory.

        Args:
            key: The memory key name.
            task_id: Task ID for scoping.
            project_id: Optional project ID.
        """
        self.engine.delete_memory(
            key_scope="task",
            key=key,
            task_id=task_id,
            project_id=project_id,
        )

    def _to_entry(self, raw: Any) -> MemoryEntry:
        """Convert raw engine output to a MemoryEntry."""
        if isinstance(raw, dict):
            return MemoryEntry.from_dict(raw)
        # Try Rust extension type conversion. from_rust reads the flat
        # PyMemoryEntry fields; if raw is some other shape (or a bug
        # regresses the conversion), log + degrade to a text-only entry
        # rather than crashing the caller — but surface the failure so it
        # isn't silently masked.
        try:
            return MemoryEntry.from_rust(raw)
        except Exception:
            logger.warning(
                "MemoryEntry.from_rust failed; degrading to text-only entry",
                exc_info=True,
            )
            return MemoryEntry(content=str(raw))


class LongTermMemory:
    """Long-term memory (project-level knowledge).

    Delegates to the Engine for storage operations. Uses the
    "project" or "global" key scopes which map to Qdrant-backed
    storage with semantic search.

    Usage:
        ltm = LongTermMemory(engine)
        ltm.write("architecture", "Microservices with event sourcing",
                  project_id="my-app", importance=0.8)
        results = ltm.search("event sourcing pattern", project_id="my-app")
    """

    def __init__(self, engine: Any):
        """Initialize LongTermMemory.

        Args:
            engine: Engine instance with memory operations and search_memory.
        """
        self.engine = engine

    def read(
        self,
        key: str,
        project_id: str | None = None,
    ) -> MemoryEntry | None:
        """Read a value from long-term memory.

        Args:
            key: The memory key name.
            project_id: Project ID for project-scoped memory.
                         If None, reads from global scope.

        Returns:
            MemoryEntry or None if not found.
        """
        scope = "project" if project_id else "global"
        raw = self.engine.read_memory(
            key_scope=scope,
            key=key,
            project_id=project_id,
            include_semantic=True,
        )
        if raw is None:
            return None
        return self._to_entry(raw)

    def write(
        self,
        key: str,
        content: str,
        project_id: str | None = None,
        content_type: str = "text",
        source_agent: str = "python",
        importance: float = 0.8,
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        """Write a value to long-term memory.

        Args:
            key: The memory key name.
            content: Content to store.
            project_id: Project ID for project-scoped memory.
                         If None, writes to global scope.
            content_type: Content type.
            source_agent: Agent that created this memory.
            importance: Importance score (0.0-1.0), default higher for long-term.
            tags: Tags for categorization.

        Returns:
            MemoryEntry with the written data.
        """
        scope = "project" if project_id else "global"
        raw = self.engine.write_memory(
            key_scope=scope,
            key=key,
            content=content,
            content_type=content_type,
            source_agent=source_agent,
            importance=importance,
            tags=tags,
            project_id=project_id,
        )
        return self._to_entry(raw)

    def search(
        self,
        query: str,
        project_id: str | None = None,
        max_results: int = 20,
        min_score: float = 0.5,
    ) -> list[MemoryEntry]:
        """Search long-term memory semantically.

        Args:
            query: Search query text.
            project_id: Project ID for project-scoped search.
                         If None, searches global scope.
            max_results: Maximum number of results.
            min_score: Minimum similarity score (0.0-1.0).

        Returns:
            List of MemoryEntry objects ranked by relevance.
        """
        scope_type = "project" if project_id else "global"
        raw_results = self.engine.search_memory(
            query=query,
            scope_type=scope_type,
            project_id=project_id,
            max_results=max_results,
            min_score=min_score,
        )

        entries = []
        for raw in raw_results:
            # search_memory returns MemorySearchResult objects
            entry_obj = getattr(raw, "entry", None)
            if entry_obj is not None:
                entries.append(self._to_entry(entry_obj))
            else:
                entries.append(self._to_entry(raw))
        return entries

    def delete(
        self,
        key: str,
        project_id: str | None = None,
    ) -> None:
        """Delete a value from long-term memory.

        Args:
            key: The memory key name.
            project_id: Project ID for project-scoped memory.
                         If None, deletes from global scope.
        """
        scope = "project" if project_id else "global"
        self.engine.delete_memory(
            key_scope=scope,
            key=key,
            project_id=project_id,
        )

    def _to_entry(self, raw: Any) -> MemoryEntry:
        """Convert raw engine output to a MemoryEntry."""
        if isinstance(raw, dict):
            return MemoryEntry.from_dict(raw)
        try:
            return MemoryEntry.from_rust(raw)
        except Exception:
            return MemoryEntry(content=str(raw))
