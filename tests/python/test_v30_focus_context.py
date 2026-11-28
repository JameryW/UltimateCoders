"""Tests for the V29 #135 focus-resumption context wiring."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from ultimate_coders.agent import focus_context as fc
from ultimate_coders.agent.focus_context import (
    MAX_LISTED,
    FocusEntry,
    SubtaskFocus,
    begin_subtask,
    finish_subtask,
    focus_block,
    focus_context_enabled,
    focus_state,
    reset_focus_context,
)


@pytest.fixture(autouse=True)
def _clean() -> None:
    reset_focus_context()
    yield
    reset_focus_context()


def _subtask(
    subtask_id: str = "s1",
    scope: str = "graph-1",
    description: str = "invalidate the cache on write",
    files: tuple[str, ...] = ("src/cache.py",),
) -> SimpleNamespace:
    return SimpleNamespace(
        id=subtask_id,
        parent_id=scope,
        description=description,
        file_constraints=list(files),
    )


class TestFlag:
    def test_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(fc.FOCUS_CONTEXT_ENV, raising=False)
        monkeypatch.setenv("UC_FOCUS_RESUMPTION", "1")
        assert focus_context_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_truthy_values(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(fc.FOCUS_CONTEXT_ENV, value)
        monkeypatch.setenv("UC_FOCUS_RESUMPTION", "true")
        assert focus_context_enabled() is True

    def test_requires_the_ledger_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(fc.FOCUS_CONTEXT_ENV, "1")
        monkeypatch.delenv("UC_FOCUS_RESUMPTION", raising=False)
        assert focus_context_enabled() is False

    def test_helpers_are_noops_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(fc.FOCUS_CONTEXT_ENV, raising=False)
        subtask = _subtask()
        assert begin_subtask(subtask) is None
        assert focus_block(subtask) is None
        assert finish_subtask(subtask) is None


class TestEntry:
    def test_validation(self) -> None:
        with pytest.raises(ValueError):
            FocusEntry(scope="", subtask_id="s1", description="x")
        with pytest.raises(ValueError):
            FocusEntry(scope="g", subtask_id="", description="x")

    def test_line_is_bounded(self) -> None:
        entry = FocusEntry(
            scope="g",
            subtask_id="s1",
            description="x" * 500,
            files=("a.py", "b.py", "c.py", "d.py"),
        )
        assert "d.py" not in entry.line()
        assert entry.to_dict()["subtask_id"] == "s1"


class TestRegistry:
    def test_register_and_retire(self) -> None:
        registry = SubtaskFocus()
        registry.begin("g", "s1", "invalidate the cache")
        registry.begin("g", "s2", "rename the loader")
        assert [entry.subtask_id for entry in registry.open_tasks("g")] == ["s2", "s1"]
        assert registry.finish("g", "s2") == 1
        assert registry.finish("g", "s2") == 1  # retiring twice is harmless
        assert registry.finish("g", "nope") == 1

    def test_scopes_are_isolated(self) -> None:
        registry = SubtaskFocus()
        registry.begin("g1", "s1", "cache work")
        registry.begin("g2", "s2", "loader work")
        assert [entry.subtask_id for entry in registry.open_tasks("g1")] == ["s1"]
        assert registry.to_dict()["scopes"] == ["g1", "g2"]

    def test_exclude_hides_the_current_subtask(self) -> None:
        registry = SubtaskFocus()
        registry.begin("g", "s1", "cache work")
        assert registry.open_tasks("g", exclude="s1") == ()

    def test_capacity_keeps_the_newest_cards(self) -> None:
        registry = SubtaskFocus(max_cards=2)
        for index in range(4):
            registry.begin("g", f"s{index}", f"task {index}")
        open_ids = {entry.subtask_id for entry in registry.open_tasks("g")}
        assert open_ids == {"s2", "s3"}
        assert registry.to_dict()["open"] == 2

    def test_stale_cards_are_pruned(self) -> None:
        registry = SubtaskFocus(max_cards=8, stale_generations=2)
        registry.begin("g", "old", "old work")
        for index in range(4):
            registry.begin("g", f"new{index}", f"new work {index}")
        assert "old" not in {entry.subtask_id for entry in registry.open_tasks("g")}

    def test_invalid_configuration(self) -> None:
        with pytest.raises(ValueError):
            SubtaskFocus(max_cards=0)
        with pytest.raises(ValueError):
            SubtaskFocus(stale_generations=0)

    def test_blank_ids_are_ignored(self) -> None:
        registry = SubtaskFocus()
        assert registry.begin("", "s1", "work") is None
        assert registry.begin("g", "  ", "work") is None
        assert registry.to_dict()["open"] == 0

    def test_reset_clears_state(self) -> None:
        registry = SubtaskFocus()
        registry.begin("g", "s1", "work")
        registry.reset()
        assert registry.to_dict() == {"open": 0, "generation": 0, "scopes": []}


class TestRender:
    def test_nothing_in_flight_returns_none(self) -> None:
        assert SubtaskFocus().render("g", "cache work") is None

    def test_related_card_is_spelled_out(self) -> None:
        registry = SubtaskFocus()
        registry.begin("g", "s1", "invalidate the cache on write", files=("src/cache.py",))
        text, meta = registry.render("g", "fix stale cache reads", exclude="s2")
        assert "Other unfinished work" in text
        assert "[s1] invalidate the cache on write" in text
        assert meta == {"open": 1, "listed": 1, "related": 1, "unrelated": 0}

    def test_unrelated_cards_collapse_to_one_line(self) -> None:
        registry = SubtaskFocus()
        registry.begin("g", "s1", "rename the protobuf loader", files=("proto/io.proto",))
        text, meta = registry.render("g", "invalidate the cache on write")
        assert "1 subtask(s) are changing unrelated files" in text
        assert "protobuf" not in text
        assert meta["unrelated"] == 1
        assert meta["listed"] == 0

    def test_listing_is_capped(self) -> None:
        registry = SubtaskFocus()
        for index in range(MAX_LISTED + 2):
            registry.begin("g", f"s{index}", f"cache work number {index}")
        text, meta = registry.render("g", "cache work", max_listed=MAX_LISTED)
        assert meta["related"] == MAX_LISTED + 2
        assert meta["listed"] == MAX_LISTED
        assert "and 2 more related subtask(s)" in text

    def test_render_is_bounded(self) -> None:
        registry = SubtaskFocus()
        for index in range(MAX_LISTED):
            registry.begin("g", f"s{index}", "cache " * 40)
        text, _ = registry.render("g", "cache", max_listed=MAX_LISTED)
        assert len(text) <= fc.MAX_RENDER_CHARS


class TestWorkerWiring:
    def test_begin_render_finish_flow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(fc.FOCUS_CONTEXT_ENV, "1")
        monkeypatch.setenv("UC_FOCUS_RESUMPTION", "1")
        first = _subtask("s1", description="invalidate the cache on write")
        second = _subtask("s2", description="repair the cache lookup tests")
        begin_subtask(first)
        begin_subtask(second)
        rendered = focus_block(second)
        assert rendered is not None
        text, meta = rendered
        assert "[s1]" in text
        assert meta["related"] == 1
        # A subtask never sees its own card.
        assert "[s2]" not in focus_block(second)[0]
        event = finish_subtask(second)
        assert event is not None
        assert event["focus_cleared"] == "s2"
        assert event["focus_open"] == 1
        assert event["phase"] == "focus resumption"
        assert focus_state()["open"] == 1

    def test_subtasks_without_a_graph_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(fc.FOCUS_CONTEXT_ENV, "1")
        monkeypatch.setenv("UC_FOCUS_RESUMPTION", "1")
        orphan = _subtask("s1", scope="")
        assert begin_subtask(orphan) is None
        assert focus_block(orphan) is None
        assert finish_subtask(orphan) is None
        assert focus_state()["open"] == 0

    def test_worker_context_items_use_the_evidence_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(fc.FOCUS_CONTEXT_ENV, "1")
        monkeypatch.setenv("UC_FOCUS_RESUMPTION", "1")
        from ultimate_coders.agent.worker import Worker

        begin_subtask(_subtask("s1", description="invalidate the cache on write"))
        worker = Worker.__new__(Worker)
        items, summary = Worker._focus_context_items(
            worker, _subtask("s2", description="repair the cache lookup tests")
        )
        assert len(items) == 1
        item = items[0]
        assert item.item_id == "focus:s2"
        assert item.source == "focus-resumption"
        assert item.stable is True
        assert "invalidate the cache on write" in item.content
        assert summary["related"] == 1

    def test_worker_context_items_are_empty_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(fc.FOCUS_CONTEXT_ENV, raising=False)
        from ultimate_coders.agent.worker import Worker

        begin_subtask(_subtask("s1"))
        worker = Worker.__new__(Worker)
        assert Worker._focus_context_items(worker, _subtask("s2")) == ([], {})

    def test_worker_finish_reports_the_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(fc.FOCUS_CONTEXT_ENV, "1")
        monkeypatch.setenv("UC_FOCUS_RESUMPTION", "1")
        from ultimate_coders.agent.worker import Worker

        begin_subtask(_subtask("s1"))
        begin_subtask(_subtask("s2"))
        worker = Worker.__new__(Worker)
        event = Worker._focus_finish(worker, _subtask("s2"))
        assert event is not None
        assert event["focus_open"] == 1
        assert Worker._focus_finish(worker, _subtask("unknown"))["focus_open"] == 1
