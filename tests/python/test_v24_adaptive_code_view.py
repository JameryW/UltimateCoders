from __future__ import annotations

import pytest
from ultimate_coders.search.adaptive_code_view import (
    ADAPTIVE_CODE_VIEW_ENV,
    AdaptiveCodeViewRouter,
    CodeCandidate,
    TaskKind,
    ViewKind,
    adaptive_code_view_enabled,
)


def candidate(path: str, **kwargs: object) -> CodeCandidate:
    return CodeCandidate(path=path, **kwargs)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ADAPTIVE_CODE_VIEW_ENV, raising=False)
    assert not adaptive_code_view_enabled()
    monkeypatch.setenv(ADAPTIVE_CODE_VIEW_ENV, "yes")
    assert adaptive_code_view_enabled()


def test_fault_localization_prefers_ast_and_deps() -> None:
    plan = AdaptiveCodeViewRouter().route(
        TaskKind.FAULT_LOCALIZATION,
        [candidate("src/a.py", has_ast=True, has_deps=True, relevance=0.8)],
    )
    assert {selection.view for selection in plan.views} == {ViewKind.AST, ViewKind.DEPS}


def test_security_patch_prefers_callgraph_then_ast() -> None:
    plan = AdaptiveCodeViewRouter().route(
        TaskKind.SECURITY_PATCH,
        [candidate("src/a.py", has_ast=True, has_callgraph=True)],
    )
    assert plan.views[0].view == ViewKind.CALLGRAPH
    assert plan.views[1].view == ViewKind.AST


def test_test_reproduction_prefers_hybrid_and_docs() -> None:
    plan = AdaptiveCodeViewRouter().route(
        TaskKind.TEST_REPRODUCTION,
        [candidate("tests/test_a.py", has_hybrid=True, has_docs=True)],
    )
    assert {selection.view for selection in plan.views} == {ViewKind.HYBRID, ViewKind.DOCS}


def test_general_task_uses_hybrid() -> None:
    plan = AdaptiveCodeViewRouter().route(
        TaskKind.GENERAL, [candidate("src/a.py", has_hybrid=True)]
    )
    assert len(plan.views) == 1
    assert plan.views[0].view == ViewKind.HYBRID


def test_budget_degradation_keeps_one_view() -> None:
    plan = AdaptiveCodeViewRouter(budget_tokens=100).route(
        TaskKind.FAULT_LOCALIZATION,
        [candidate("src/a.py", token_estimate=5000, has_ast=True, has_deps=True)],
    )
    assert plan.degraded
    assert len(plan.views) == 1
    assert plan.views[0].token_estimate == 100


def test_unknown_task_is_rejected() -> None:
    with pytest.raises(ValueError):
        AdaptiveCodeViewRouter().route("unknown", [])


def test_invalid_candidate_is_rejected() -> None:
    with pytest.raises(ValueError):
        CodeCandidate("", token_estimate=10)
    with pytest.raises(ValueError):
        CodeCandidate("src/a.py", token_estimate=0)


def test_empty_candidates_returns_empty_plan() -> None:
    plan = AdaptiveCodeViewRouter().route(TaskKind.GENERAL, [])
    assert plan.views == ()
    assert not plan.degraded


def test_render_is_stable() -> None:
    plan = AdaptiveCodeViewRouter().route(
        TaskKind.GENERAL,
        [candidate("src/a.py", has_hybrid=True, token_estimate=50)],
    )
    assert "src/a.py" in plan.render()
    assert "[hybrid]" in plan.render()
