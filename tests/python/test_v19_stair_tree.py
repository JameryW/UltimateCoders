"""Tests for the V19 STAIR hierarchical plan tree (#74)."""

from __future__ import annotations

import pytest
from ultimate_coders.search.stair_tree import (
    STAIR_TREE_ENV,
    StairTree,
    TrajectoryLevel,
    stair_tree_enabled,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STAIR_TREE_ENV, raising=False)
    assert not stair_tree_enabled()
    monkeypatch.setenv(STAIR_TREE_ENV, "1")
    assert stair_tree_enabled()


def test_add_trajectory_creates_all_levels() -> None:
    tree = StairTree()
    nodes = tree.add_trajectory(
        "Fix cache invalidation",
        ["Update cache store", "Refresh API"],
        ["grep invalidate", "read cache.py"],
    )
    assert len(nodes) == 5
    assert [node.level for node in nodes] == [
        TrajectoryLevel.STRATEGY,
        TrajectoryLevel.SUBGOAL,
        TrajectoryLevel.SUBGOAL,
        TrajectoryLevel.DIAGNOSTIC,
        TrajectoryLevel.DIAGNOSTIC,
    ]
    assert all(node.parent_id == nodes[0].id for node in nodes[1:])


def test_search_returns_a_mixed_granularity_plan() -> None:
    tree = StairTree()
    tree.add_trajectory(
        "Fix cache invalidation",
        ["Update cache store"],
        ["grep invalidate"],
        tags=("cache",),
    )
    plan = tree.search("cache invalidation", tags=("cache",), max_nodes=3)
    assert plan.strategy is not None
    assert len(plan.subgoals) == 1
    assert len(plan.diagnostics) == 1
    assert plan.strategy.text == "Fix cache invalidation"
    assert "Strategy:" in plan.render()
    assert plan.score > 0


def test_failed_trajectories_are_excluded_by_default() -> None:
    tree = StairTree()
    tree.add_trajectory(
        "Bad strategy",
        ["Bad step"],
        ["bad diagnostic"],
        tags=("cache",),
        success=False,
    )
    tree.add_trajectory(
        "Good strategy",
        ["Good step"],
        ["good diagnostic"],
        tags=("cache",),
    )
    plan = tree.search("cache", tags=("cache",))
    assert plan.strategy is not None
    assert plan.strategy.text == "Good strategy"


def test_failed_trajectories_can_be_included_explicitly() -> None:
    tree = StairTree()
    tree.add_trajectory(
        "Bad strategy",
        ["Bad step"],
        ["bad diagnostic"],
        tags=("cache",),
        success=False,
    )
    plan = tree.search("cache", tags=("cache",), include_failed=True)
    assert plan.strategy is not None
    assert plan.strategy.text == "Bad strategy"


def test_max_nodes_bounds_the_plan() -> None:
    tree = StairTree()
    tree.add_trajectory(
        "Fix cache",
        [f"Step {i}" for i in range(5)],
        [f"Diagnostic {i}" for i in range(5)],
    )
    plan = tree.search("cache", max_nodes=4)
    assert len(plan.nodes) <= 4


def test_unknown_parent_is_rejected() -> None:
    tree = StairTree()
    with pytest.raises(ValueError, match="unknown parent"):
        tree.add_node(
            TrajectoryLevel.SUBGOAL,
            "Step",
            parent_id="missing",
        )


def test_plan_render_contains_all_selected_nodes() -> None:
    tree = StairTree()
    tree.add_trajectory("Fix cache", ["Update store"], ["grep cache"])
    plan = tree.search("cache", max_nodes=3)
    rendered = plan.render()
    assert "Strategy: Fix cache" in rendered
    assert "Subgoal 1: Update store" in rendered
    assert "Diagnostic 1: grep cache" in rendered
