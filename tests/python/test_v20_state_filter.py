from __future__ import annotations

import pytest
from ultimate_coders.search.state_filter import (
    STATE_FILTER_ENV,
    ExplicitStateFilter,
    ParsedEvent,
    StateFilterError,
    StateTransition,
    parse_event_line,
    state_filter_enabled,
)


def test_flag_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STATE_FILTER_ENV, "true")
    assert state_filter_enabled()
    monkeypatch.setenv(STATE_FILTER_ENV, "0")
    assert not state_filter_enabled()


def test_parse_set_and_delete_lines() -> None:
    event = parse_event_line("cache = warm @12")
    assert event.key == "cache"
    assert event.value == "warm"
    assert event.timestamp == 12
    deleted = parse_event_line("cache=delete@13")
    assert deleted.value is None


def test_apply_and_read_entry() -> None:
    state = ExplicitStateFilter()
    assert state.apply(StateTransition("k", "v", 1))
    entry = state.get("k")
    assert entry is not None
    assert entry.value == "v"


def test_stale_transition_is_rejected() -> None:
    state = ExplicitStateFilter()
    state.apply(StateTransition("k", "new", 10))
    assert not state.apply(StateTransition("k", "old", 9))
    assert state.stats.rejected_stale == 1
    assert state.get("k").value == "new"  # type: ignore[union-attr]


def test_delete_removes_entry() -> None:
    state = ExplicitStateFilter()
    state.apply(StateTransition("k", "v", 1))
    assert state.apply(StateTransition("k", None, 2, "delete"))
    assert state.get("k") is None
    assert state.stats.deleted == 1


def test_dependency_keys_are_retained() -> None:
    state = ExplicitStateFilter()
    state.apply_parsed(ParsedEvent("k", "v", 1, ("a", "b")))
    assert state.dependencies("k") == ("a", "b")


def test_apply_many_skips_stale_events() -> None:
    state = ExplicitStateFilter()
    events = [ParsedEvent("k", "one", 1), ParsedEvent("k", "two", 2), ParsedEvent("k", "old", 1)]
    assert state.apply_many(events) == 2
    assert state.get("k").value == "two"  # type: ignore[union-attr]


def test_eviction_is_bounded_and_deterministic() -> None:
    state = ExplicitStateFilter(max_keys=2)
    state.apply_many([ParsedEvent("a", "1", 1), ParsedEvent("b", "2", 2), ParsedEvent("c", "3", 3)])
    snapshot = state.snapshot()
    assert list(snapshot) == ["b", "c"]
    assert state.stats.evicted == 1


def test_snapshot_and_dict_are_sorted() -> None:
    state = ExplicitStateFilter()
    state.apply_many([ParsedEvent("z", "1", 1), ParsedEvent("a", "2", 2)])
    assert list(state.snapshot()) == ["a", "z"]
    payload = state.to_dict()
    assert payload["max_keys"] == 128
    assert payload["stats"]["applied"] == 2


def test_invalid_lines_and_empty_keys_are_rejected() -> None:
    with pytest.raises(StateFilterError):
        parse_event_line("")
    with pytest.raises(StateFilterError):
        parse_event_line("key=value")
    with pytest.raises(StateFilterError):
        parse_event_line("=value@1")
    with pytest.raises(StateFilterError):
        ExplicitStateFilter(max_keys=0)
    with pytest.raises(StateFilterError):
        ParsedEvent("", "x", 1)
