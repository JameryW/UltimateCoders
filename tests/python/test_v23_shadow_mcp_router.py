from __future__ import annotations

import pytest
from ultimate_coders.agent.shadow_mcp_router import (
    SHADOW_MCP_ROUTER_ENV,
    PollingGuard,
    ShadowMcpRouter,
    SodsFeatures,
    ToolCandidate,
    shadow_mcp_router_enabled,
)


def candidate(
    name: str,
    *,
    tp: float = 0.9,
    op: float = 0.1,
    drift: float = 0.1,
    security: bool = False,
    audit: bool = False,
    shadowed: bool = False,
    allowed: bool = True,
) -> ToolCandidate:
    return ToolCandidate(
        name,
        SodsFeatures(tp, op, drift),
        security_critical=security,
        audit_required=audit,
        shadowed=shadowed,
        allowed=allowed,
    )


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SHADOW_MCP_ROUTER_ENV, raising=False)
    assert not shadow_mcp_router_enabled()
    monkeypatch.setenv(SHADOW_MCP_ROUTER_ENV, "true")
    assert shadow_mcp_router_enabled()


def test_sods_score_prefers_task_preserving_low_pressure_routes() -> None:
    good = SodsFeatures(1.0, 0.0, 0.0)
    bad = SodsFeatures(0.0, 1.0, 1.0)
    assert good.score == 1.0
    assert bad.score == 0.0


def test_shadowed_candidate_is_blocked() -> None:
    decision = ShadowMcpRouter().route([candidate("bad", shadowed=True), candidate("ok")])
    assert decision.selected == "ok"
    assert decision.blocked == ("bad",)


def test_mandatory_security_tool_cannot_be_skipped() -> None:
    decision = ShadowMcpRouter().route(
        [candidate("policy_check", security=True, shadowed=True), candidate("read")]
    )
    assert not decision.ok
    assert decision.skipped_security == ("policy_check",)


def test_security_tool_is_preserved_in_route() -> None:
    decision = ShadowMcpRouter().route(
        [candidate("policy_check", security=True, tp=0.99), candidate("read", tp=0.5)]
    )
    assert decision.ok
    assert decision.selected == "policy_check"


def test_low_score_candidate_is_ineligible() -> None:
    router = ShadowMcpRouter(minimum_score=0.9)
    decision = router.route([candidate("weak", tp=0.1)])
    assert decision.selected is None


def test_empty_candidates_are_rejected() -> None:
    decision = ShadowMcpRouter().route([])
    assert decision.selected is None
    assert decision.reason == "no candidates"


def test_polling_backoff_uses_repeated_state_digest() -> None:
    guard = PollingGuard(base_interval_steps=1)
    assert guard.observe(0, "same") == 1
    assert guard.observe(1, "same") == 3
    assert guard.observe(3, "same") == 7


def test_polling_guard_stops_after_completion_and_bound() -> None:
    guard = PollingGuard(completion_bound_steps=5)
    assert guard.observe(0, "a") == 1
    assert guard.observe(1, "b") == 2
    assert guard.observe(6, "c") is None
    assert guard.observe(7, "d", task_complete=True) is None
    assert guard.done


def test_polling_guard_reset_clears_state() -> None:
    guard = PollingGuard()
    guard.observe(0, "a")
    guard.reset()
    assert not guard.done
    assert guard.observe(0, "a") == 1
