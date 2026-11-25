"""Tests for the GrepMem recall journal wiring in the memory layer (v29 #133).

The journal is a projection: ``write`` still goes to the engine, and a written
entry is additionally appended to a lossless regex log so a process can recall
it when the semantic index returns nothing.  Every test here runs with the
``UC_GREPMEM_MEMORIX`` flag explicitly set or cleared, because the flag is the
whole contract -- with it off the layer must behave exactly as before.
"""

from __future__ import annotations

import json

import pytest
from ultimate_coders.memory.grepmem_memorix import GREPMEM_MEMORIX_ENV
from ultimate_coders.memory.memory import (
    LongTermMemory,
    MemoryEntry,
    MemoryKey,
    ShortTermMemory,
    lexical_pattern,
    recall_journal,
    reset_recall_journal,
)


class _RecordingEngine:
    """Minimal engine double: records writes, returns searchable results."""

    def __init__(self, search_results: list[dict] | None = None) -> None:
        self.writes: list[dict] = []
        self.search_calls: list[dict] = []
        self.search_results = search_results or []

    def write_memory(self, **kwargs) -> dict:
        self.writes.append(kwargs)
        return {
            "id": f"mem-{len(self.writes)}",
            "key": {
                "scope": kwargs["key_scope"],
                "key": kwargs["key"],
                "task_id": kwargs.get("task_id"),
                "project_id": kwargs.get("project_id"),
            },
            "content": kwargs["content"],
            "content_type": kwargs.get("content_type", "text"),
            "source_agent": kwargs.get("source_agent", "python"),
            "importance": kwargs.get("importance", 0.5),
            "tags": kwargs.get("tags") or [],
            "created_at": 0,
            "updated_at": 0,
        }

    def read_memory(self, **kwargs):
        return None

    def delete_memory(self, **kwargs) -> None:
        return None

    def search_memory(self, **kwargs):
        self.search_calls.append(kwargs)
        return self.search_results


@pytest.fixture(autouse=True)
def _fresh_journal() -> None:
    reset_recall_journal()
    yield
    reset_recall_journal()


class TestFlagGating:
    def test_journal_is_absent_when_the_flag_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(GREPMEM_MEMORIX_ENV, raising=False)
        assert recall_journal() is None

        engine = _RecordingEngine()
        ShortTermMemory(engine).write("decisions", "use postgres", task_id="t1")
        assert len(engine.writes) == 1
        assert ShortTermMemory(engine).recall("postgres") == []

    def test_journal_is_created_lazily_when_armed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        assert recall_journal() is not None
        assert recall_journal() is recall_journal()

    def test_reset_drops_the_journal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        journal = recall_journal()
        assert journal is not None
        journal.remember("hello")
        reset_recall_journal()
        assert recall_journal() is not journal
        assert len(recall_journal().log) == 0  # type: ignore[union-attr]


class TestWriteJournal:
    def test_short_term_write_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        engine = _RecordingEngine()
        stm = ShortTermMemory(engine)
        stm.write("decisions", "Use PostgreSQL for the ledger", task_id="t1")

        hits = stm.recall("PostgreSQL")
        assert len(hits) == 1
        assert hits[0].content == "Use PostgreSQL for the ledger"
        assert hits[0].key.scope == "task"
        assert hits[0].key.task_id == "t1"
        assert hits[0].key.key == "decisions"
        assert hits[0].id == "mem-1"

    def test_long_term_write_is_recorded_with_project_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        ltm = LongTermMemory(_RecordingEngine())
        ltm.write("architecture", "Event sourcing with outbox", project_id="app")

        hits = ltm.recall("outbox")
        assert len(hits) == 1
        assert hits[0].key.scope == "project"
        assert hits[0].key.project_id == "app"
        assert hits[0].tags == []

    def test_global_scope_is_recorded_as_global(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        ltm = LongTermMemory(_RecordingEngine())
        ltm.write("convention", "two-space indentation")

        hits = ltm.recall("indentation")
        assert hits[0].key.scope == "global"

    def test_journal_records_extras(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        engine = _RecordingEngine()
        ShortTermMemory(engine).write(
            "attempt",
            "retry with backoff",
            task_id="t2",
            content_type="code",
            source_agent="worker",
            importance=0.9,
            tags=["retry"],
        )

        record = recall_journal().log.all()[0]  # type: ignore[union-attr]
        payload = json.loads(record.text)
        assert payload["content_type"] == "code"
        assert payload["source_agent"] == "worker"
        assert payload["importance"] == pytest.approx(0.9)
        assert payload["tags"] == ["retry"]
        assert record.source == "memory:task"

    def test_engine_write_still_happens_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        engine = _RecordingEngine()
        ShortTermMemory(engine).write("k", "v", task_id="t1")
        assert engine.writes[0]["content"] == "v"

    def test_full_journal_never_fails_the_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        journal = recall_journal()
        assert journal is not None
        journal.log = type(journal.log)(max_entries=1)
        journal.log.append("occupied")

        engine = _RecordingEngine()
        entry = ShortTermMemory(engine).write("k", "v", task_id="t1")
        assert entry.content == "v"
        assert engine.writes


class TestLexicalFallback:
    def test_search_falls_back_to_the_journal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        engine = _RecordingEngine(search_results=[])
        ltm = LongTermMemory(engine)
        ltm.write("architecture", "Outbox pattern keeps events durable", project_id="app")

        hits = ltm.search("outbox durability", project_id="app")
        assert [hit.content for hit in hits] == ["Outbox pattern keeps events durable"]

    def test_semantic_results_win_over_the_journal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        engine = _RecordingEngine(
            search_results=[
                {
                    "id": "remote-1",
                    "key": {"scope": "project", "key": "architecture", "project_id": "app"},
                    "content": "semantic hit",
                }
            ]
        )
        ltm = LongTermMemory(engine)
        ltm.write("architecture", "journal hit", project_id="app")

        hits = ltm.search("hit", project_id="app")
        assert [hit.content for hit in hits] == ["semantic hit"]

    def test_no_fallback_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(GREPMEM_MEMORIX_ENV, raising=False)
        engine = _RecordingEngine(search_results=[])
        ltm = LongTermMemory(engine)
        ltm.write("architecture", "journal hit", project_id="app")

        assert ltm.search("journal", project_id="app") == []
        assert engine.search_calls

    def test_short_query_does_not_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        ltm = LongTermMemory(_RecordingEngine())
        ltm.write("architecture", "outbox pattern", project_id="app")
        assert lexical_pattern("a of to") == ""
        assert ltm.search("a of to", project_id="app") == []

    def test_pattern_is_case_insensitive_and_deduped(self) -> None:
        pattern = lexical_pattern("Outbox outbox ledger")
        assert pattern.startswith("(?i)")
        assert pattern.count("outbox") == 1
        assert "ledger" in pattern

    def test_invalid_stored_record_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        journal = recall_journal()
        assert journal is not None
        journal.remember("not json at all", "memory:task")

        stm = ShortTermMemory(_RecordingEngine())
        assert stm.recall("json") == []


class TestEntryRoundTrip:
    def test_recalled_entry_is_a_memory_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREPMEM_MEMORIX_ENV, "1")
        ShortTermMemory(_RecordingEngine()).write(
            "decisions", "ship it", task_id="t9", tags=["ship"]
        )

        hits = ShortTermMemory(_RecordingEngine()).recall("ship")
        assert all(isinstance(hit, MemoryEntry) for hit in hits)
        assert hits[0].key == MemoryKey(scope="task", key="decisions", task_id="t9")
