"""Tests for V22 ledger item #94 (CASE-Bench safeguard context)."""

from __future__ import annotations

import pytest
from ultimate_coders.search.case_safeguard_ctx import (
    CallEdge,
    CaseSafeguardContext,
    CaseSafeguardError,
    GuardFunction,
    GuardIndex,
    GuardKind,
)


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_CASE_SAFEGUARD_CTX", raising=False)
    from ultimate_coders.search.case_safeguard_ctx import case_safeguard_ctx_enabled

    assert case_safeguard_ctx_enabled() is False


def _index() -> GuardIndex:
    index = GuardIndex()
    index.add_guard(GuardFunction("check_auth", "src/auth.py", GuardKind.AUTH))
    index.add_guard(GuardFunction("sanitize", "src/input.py", GuardKind.INPUT_SANITIZATION))
    index.add_guard(GuardFunction("bound", "src/bounds.py", GuardKind.BOUNDS_CHECK))
    index.add_edge(CallEdge("check_auth", "handler"))
    index.add_edge(CallEdge("sanitize", "handler"))
    index.add_edge(CallEdge("bound", "sanitize"))
    return index


def test_upstream_guards_are_ordered_by_kind_then_depth() -> None:
    context = CaseSafeguardContext(_index()).build("handler")
    assert [hit.guard.kind for hit in context.guards] == [
        GuardKind.AUTH,
        GuardKind.INPUT_SANITIZATION,
        GuardKind.BOUNDS_CHECK,
    ]


def test_depth_limit_excludes_deep_guards() -> None:
    context = CaseSafeguardContext(_index()).build("handler", max_depth=1)
    symbols = [hit.guard.symbol for hit in context.guards]
    assert "check_auth" in symbols
    assert "bound" not in symbols


def test_cycle_is_safe_and_deduped() -> None:
    index = GuardIndex()
    index.add_guard(GuardFunction("check_auth", "auth.py", GuardKind.AUTH))
    index.add_edge(CallEdge("a", "b"))
    index.add_edge(CallEdge("b", "a"))
    context = CaseSafeguardContext(index).build("a")
    assert len(context.guards) == 0


def test_guard_context_render_starts_with_header() -> None:
    context = CaseSafeguardContext(_index()).build("handler")
    rendered = context.render()
    assert rendered.startswith("## Safety guards (must remain)")
    assert "Target: handler" in rendered


def test_coverage_is_zero_without_guards() -> None:
    assert CaseSafeguardContext(GuardIndex()).coverage("handler") == 0.0


def test_coverage_is_one_with_an_upstream_guard() -> None:
    assert CaseSafeguardContext(_index()).coverage("handler") == 1.0


def test_duplicate_guard_is_rejected() -> None:
    index = GuardIndex()
    guard = GuardFunction("check_auth", "auth.py", GuardKind.AUTH)
    index.add_guard(guard)
    with pytest.raises(CaseSafeguardError):
        index.add_guard(guard)


def test_self_call_edge_is_rejected() -> None:
    with pytest.raises(CaseSafeguardError):
        CallEdge("same", "same")


def test_negative_depth_is_rejected() -> None:
    with pytest.raises(CaseSafeguardError):
        GuardIndex().upstream_guards("x", max_depth=-1)
