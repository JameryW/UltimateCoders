"""Tests for MemoryEntry.from_rust (Rust PyMemoryEntry → Python conversion).

Regression: from_rust was written against an old enum-based PyMemoryEntry
shape (content.text, metadata, key-as-enum). The actual PyMemoryEntry
exposes FLAT fields: key_scope, key, task_id, project_id, content_type,
content, source_agent, importance, tags, created_at/updated_at (i64 millis).
Every Rust-sourced entry was silently mis-typed (content_type forced to
"text") and de-scoped (all keys treated as global, task_id/project_id lost).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from ultimate_coders.memory.memory import MemoryEntry, MemoryKey


def _py_entry(**overrides):
    """Build a fake PyMemoryEntry (flat fields matching the Rust pyclass)."""
    base = dict(
        id="mem-1",
        key_scope="task",
        key="summary",
        task_id="t-123",
        project_id=None,
        content_type="code",
        content="print('hi')",
        language="python",
        file_path="/p/f.py",
        uri=None,
        description=None,
        source_agent="worker-1",
        importance=0.9,
        tags=["a", "b"],
        created_at=1700000000000,
        updated_at=1700000005000,
        version=5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestFromRust:
    def test_reads_flat_fields(self):
        entry = MemoryEntry.from_rust(_py_entry())
        assert entry.id == "mem-1"
        assert entry.key == MemoryKey(
            scope="task", key="summary", task_id="t-123", project_id=None,
        )
        assert entry.content == "print('hi')"
        # content_type must come from the field, NOT be hardcoded "text".
        assert entry.content_type == "code"
        assert entry.source_agent == "worker-1"
        assert entry.importance == pytest.approx(0.9)
        assert entry.tags == ["a", "b"]

    def test_preserves_task_scope_and_qualifiers(self):
        """Regression: all entries were de-scoped to 'global' because the
        old code string-matched the key for 'Task' (never matched)."""
        entry = MemoryEntry.from_rust(_py_entry(key_scope="task", task_id="t-9"))
        assert entry.key.scope == "task"
        assert entry.key.task_id == "t-9"

    def test_preserves_project_scope(self):
        entry = MemoryEntry.from_rust(_py_entry(
            key_scope="project", task_id=None, project_id="proj-1",
        ))
        assert entry.key.scope == "project"
        assert entry.key.project_id == "proj-1"

    def test_global_scope(self):
        entry = MemoryEntry.from_rust(_py_entry(
            key_scope="global", task_id=None, project_id=None,
        ))
        assert entry.key.scope == "global"

    def test_i64_millis_converted_to_datetime(self):
        """created_at/updated_at are i64 epoch millis on the Rust side."""
        from datetime import datetime, timezone
        entry = MemoryEntry.from_rust(_py_entry(
            created_at=1700000000000, updated_at=1700000005000,
        ))
        assert entry.created_at == datetime.fromtimestamp(1700000000.0, tz=timezone.utc)
        assert entry.updated_at == datetime.fromtimestamp(1700000005.0, tz=timezone.utc)

    def test_zero_or_missing_timestamps_yield_none(self):
        entry = MemoryEntry.from_rust(_py_entry(created_at=0, updated_at=0))
        assert entry.created_at is None
        assert entry.updated_at is None

    def test_structured_content_type_preserved(self):
        """Regression: content_type was hardcoded 'text', losing structured/
        diff/reference types entirely."""
        entry = MemoryEntry.from_rust(_py_entry(
            content_type="structured", content='{"k": 1}',
        ))
        assert entry.content_type == "structured"
        assert entry.content == '{"k": 1}'

    def test_missing_fields_fall_back_safely(self):
        raw = SimpleNamespace()  # no attributes at all
        entry = MemoryEntry.from_rust(raw)
        assert entry.key.scope == "global"
        assert entry.content_type == "text"
        assert entry.tags == []


class TestF61ImportanceAndSearch:
    def test_importance_zero_preserved(self):
        """importance=0.0 is legitimate — the old `x or 0.5` rewrote it to
        0.5, shifting long-term promotion behavior."""
        entry = MemoryEntry.from_rust(_py_entry(importance=0.0))
        assert entry.importance == 0.0

    def test_importance_missing_defaults_to_half(self):
        raw = SimpleNamespace()  # no importance attribute at all
        entry = MemoryEntry.from_rust(raw)
        assert entry.importance == pytest.approx(0.5)

    def test_search_handles_none_results(self):
        """Engine returning None (unavailable) must not raise TypeError."""
        from unittest.mock import MagicMock

        from ultimate_coders.memory.memory import LongTermMemory

        engine = MagicMock()
        engine.search_memory = MagicMock(return_value=None)
        ltm = LongTermMemory(engine)
        assert ltm.search("anything") == []
