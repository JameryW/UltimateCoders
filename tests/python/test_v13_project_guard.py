"""Tests for the v13 ProjectGuard progressive specification layer (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.project_guard import (
    PROJECT_GUARD_ENABLED_ENV,
    AtomicComponent,
    ComponentState,
    NormKind,
    ProjectGuard,
    project_guard_enabled,
)


def component(component_id: str, text: str, **overrides: object) -> AtomicComponent:
    return AtomicComponent(id=component_id, text=text, **overrides)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROJECT_GUARD_ENABLED_ENV, raising=False)
    assert project_guard_enabled() is False
    monkeypatch.setenv(PROJECT_GUARD_ENABLED_ENV, "true")
    assert project_guard_enabled() is True


def test_component_derives_keywords_and_validates_fields() -> None:
    item = component("c1", "cache invalidation must be atomic")
    assert item.keywords == ("cache", "invalidation", "must", "atomic")
    assert item.kind is NormKind.REQUIREMENT
    with pytest.raises(ValueError):
        AtomicComponent(id=" ", text="cache invalidation")
    with pytest.raises(ValueError):
        AtomicComponent(id="c2", text=" ")
    with pytest.raises(ValueError):
        AtomicComponent(id="c3", text="a b")


def test_disclosure_adds_components_and_tracks_dependency_conflicts() -> None:
    guard = ProjectGuard()
    first = guard.disclose(
        1, [component("invalidate", "cache invalidation", dependencies=("store",))]
    )
    assert first.added == ("invalidate",)
    assert first.superseded == ()
    assert [edge.target for edge in first.conflicts] == ["store"]

    second = guard.disclose(2, [component("store", "durable cache store")])
    assert second.added == ("store",)
    assert second.conflicts == ()
    assert guard.conflict_graph() == ()
    assert [item.id for item in guard.active_components()] == ["invalidate", "store"]


def test_same_id_supersedes_the_previous_component() -> None:
    guard = ProjectGuard()
    guard.disclose(1, [component("cache", "cache invalidation on write")])
    result = guard.disclose(2, [component("cache", "cache invalidation on read")])
    assert result.added == ()
    assert result.superseded == ("cache",)
    state = guard.component_state("cache")
    assert isinstance(state, ComponentState)
    assert state.status == "active"
    assert state.disclosed_turn == 2
    assert "read" in state.component.text


def test_turns_are_positive_monotonic_and_bounded() -> None:
    guard = ProjectGuard(max_turns=5)
    with pytest.raises(ValueError):
        guard.disclose(0, [component("a", "cache invalidation")])
    guard.disclose(2, [component("a", "cache invalidation")])
    with pytest.raises(ValueError):
        guard.disclose(2, [component("b", "cache warmup")])
    with pytest.raises(ValueError):
        guard.disclose(6, [component("c", "cache eviction")])


def test_scan_reports_missing_required_components() -> None:
    guard = ProjectGuard()
    guard.disclose(
        1,
        [
            component("invalidate", "cache invalidation"),
            component("metrics", "emit cache metrics"),
        ],
    )
    scan = guard.scan(1, "the cache is invalidated on write")
    assert scan.faithful is False
    assert scan.matched == ("invalidate",)
    assert scan.missing == ("metrics",)
    assert scan.fidelity == pytest.approx(0.5)
    assert scan.required_count == 2


def test_scan_matches_all_required_evidence_and_optional_components() -> None:
    guard = ProjectGuard()
    guard.disclose(
        1,
        [
            component("invalidate", "cache invalidation"),
            component("metrics", "emit cache metrics"),
            component("docs", "update docs", required=False),
        ],
    )
    scan = guard.scan(
        1,
        "cache invalidation and emit metrics",
        changed_paths=["src/cache.py"],
        tests=["tests/test_cache.py"],
    )
    assert scan.faithful is True
    assert scan.fidelity == pytest.approx(1.0)
    assert scan.required_count == 2
    assert scan.changed_paths == ("src/cache.py",)
    assert scan.missing_count == 0


def test_unresolved_dependency_makes_scan_unfaithful() -> None:
    guard = ProjectGuard()
    guard.disclose(
        1, [component("invalidate", "cache invalidation", dependencies=("store",))]
    )
    scan = guard.scan(1, "cache invalidation")
    assert scan.fidelity == pytest.approx(1.0)
    assert scan.faithful is False
    assert [edge.target for edge in scan.conflicts] == ["store"]


def test_scan_history_is_bounded() -> None:
    guard = ProjectGuard(max_scans=2)
    for turn in range(1, 4):
        guard.scan(turn, "cache invalidation")
    assert [scan.turn for scan in guard.scans] == [2, 3]
    assert guard.last_turn == 3


def test_snapshot_exposes_bounded_state() -> None:
    guard = ProjectGuard()
    guard.disclose(1, [component("invalidate", "cache invalidation")])
    guard.scan(1, "cache invalidation")
    snapshot = guard.to_dict()
    assert snapshot["max_turns"] == 60
    assert snapshot["last_turn"] == 1
    assert len(snapshot["active"]) == 1
    assert len(snapshot["scans"]) == 1
