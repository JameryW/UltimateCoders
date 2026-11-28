"""Tests for the v13 RepoAtlas evolving repository view (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.search.repo_atlas_view import (
    REPO_ATLAS_VIEW_ENABLED_ENV,
    RepoAtlasController,
    RepoGraph,
    RepoNode,
    ViewKind,
    repo_atlas_view_enabled,
)


def make_graph() -> RepoGraph:
    return RepoGraph(
        (
            RepoNode(
                "api",
                "src/api.py",
                symbols=("route",),
                signatures=("def route()",),
                edges=("service",),
            ),
            RepoNode(
                "service",
                "src/service.py",
                symbols=("compute", "validate"),
                signatures=("def compute(x)",),
                edges=("store", "api"),
            ),
            RepoNode(
                "store",
                "src/store.py",
                symbols=("load",),
                signatures=("def load(key)",),
                edges=("service",),
            ),
            RepoNode("test-api", "tests/test_api.py", symbols=("test_route",), edges=("api",)),
        )
    )


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REPO_ATLAS_VIEW_ENABLED_ENV, raising=False)
    assert repo_atlas_view_enabled() is False
    monkeypatch.setenv(REPO_ATLAS_VIEW_ENABLED_ENV, "1")
    assert repo_atlas_view_enabled() is True


def test_pre_edit_selection_projects_topology_and_source_index() -> None:
    controller = RepoAtlasController(make_graph())
    selection = controller.select(seed="api")
    assert selection.substantive_edit is False
    assert selection.replaced_previous is False
    assert [view.kind for view in selection.views] == [
        ViewKind.TOPOLOGY,
        ViewKind.SOURCE_INDEX,
    ]
    assert selection.views[0].refs == ("api", "service", "store")
    assert "ref api | src/api.py" in selection.views[1].rendered
    assert "topology" in selection.views[0].rendered


def test_post_edit_selection_drops_the_topology_view() -> None:
    controller = RepoAtlasController(make_graph())
    selection = controller.select(
        substantive_edit=True, active_paths=["src/service.py"]
    )
    assert selection.substantive_edit is True
    assert [view.kind for view in selection.views] == [ViewKind.LOCAL_TEXT]
    assert selection.views[0].refs == ("service", "store", "api")
    assert "topology" not in selection.views[0].rendered
    assert "src/service.py" in selection.views[0].rendered


def test_refresh_replaces_the_previous_projection() -> None:
    controller = RepoAtlasController(make_graph())
    first = controller.select(seed="api")
    second = controller.select(seed="api")
    assert first.refresh_count == 1
    assert second.refresh_count == 2
    assert second.replaced_previous is True
    assert controller.current[0].refs == first.views[0].refs
    assert len(controller.current) == 2


def test_stable_reference_keys_survive_refresh() -> None:
    controller = RepoAtlasController(make_graph())
    first = controller.select(seed="api")
    second = controller.select(seed="api")
    assert first.views[1].refs == second.views[1].refs
    assert first.views[1].refs == ("api", "service", "store")


def test_local_text_expands_one_hop_from_the_active_patch() -> None:
    controller = RepoAtlasController(make_graph())
    selection = controller.select(
        substantive_edit=True, active_paths=["src/service.py"]
    )
    assert selection.views[0].refs == ("service", "store", "api")


def test_budget_shaping_marks_degradation() -> None:
    controller = RepoAtlasController(make_graph(), budget_tokens=4)
    selection = controller.select(seed="api")
    topology = selection.views[0]
    assert topology.degraded is True
    assert topology.token_estimate <= 4


def test_post_edit_without_an_active_path_uses_the_first_node() -> None:
    controller = RepoAtlasController(make_graph())
    selection = controller.select(substantive_edit=True)
    assert selection.views[0].refs[0] == "api"


def test_graph_rejects_duplicate_ids_and_unknown_edges() -> None:
    with pytest.raises(ValueError):
        RepoGraph(
            (
                RepoNode("a", "src/a.py"),
                RepoNode("a", "src/b.py"),
            )
        )
    with pytest.raises(ValueError):
        RepoGraph((RepoNode("a", "src/a.py", edges=("missing",)),))


def test_select_rejects_unknown_seed() -> None:
    with pytest.raises(KeyError):
        RepoAtlasController(make_graph()).select(seed="missing")
