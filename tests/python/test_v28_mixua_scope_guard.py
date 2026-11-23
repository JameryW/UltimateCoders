from __future__ import annotations

from ultimate_coders.memory.mixua_scope_guard import (
    MemoryRecord,
    MixUaScopeGuard,
    RiskTier,
    Scope,
    TaskContext,
    mixua_scope_guard_enabled,
)


def test_flag_is_opt_in(monkeypatch):
    monkeypatch.delenv("UC_MIXUA_SCOPE_GUARD", raising=False)
    assert mixua_scope_guard_enabled() is False
    monkeypatch.setenv("UC_MIXUA_SCOPE_GUARD", "on")
    assert mixua_scope_guard_enabled() is True


def test_same_user_agent_memory_is_visible():
    guard = MixUaScopeGuard()
    context = TaskContext("u1", "a1", RiskTier.LOW)
    record = MemoryRecord("m1", Scope.USER_AGENT, RiskTier.LOW, "use cache", "u1", "a1")
    report = guard.evaluate((record,), context)
    assert report.allowed == (record,)
    assert report.clean
    assert report.isolation_ratio == 1.0


def test_cross_user_memory_is_blocked():
    guard = MixUaScopeGuard()
    context = TaskContext("u1", "a1", RiskTier.LOW)
    record = MemoryRecord("m1", Scope.USER, RiskTier.LOW, "preference", "u2")
    report = guard.evaluate((record,), context)
    assert report.blocked == (record,)
    assert report.decisions[0].reason == "identity_mismatch"


def test_cross_agent_memory_is_blocked():
    guard = MixUaScopeGuard()
    context = TaskContext("u1", "a1", RiskTier.LOW)
    record = MemoryRecord("m1", Scope.AGENT, RiskTier.LOW, "agent tip", "", "a2")
    report = guard.evaluate((record,), context)
    assert report.decisions[0].reason == "identity_mismatch"


def test_global_memory_is_visible_when_risk_matches():
    guard = MixUaScopeGuard()
    context = TaskContext("u1", "a1", RiskTier.MEDIUM)
    record = MemoryRecord("m1", Scope.GLOBAL, RiskTier.MEDIUM, "run formatter")
    assert guard.evaluate((record,), context).allowed == (record,)


def test_low_risk_memory_is_fenced_out_of_a_high_risk_task():
    guard = MixUaScopeGuard()
    context = TaskContext("u1", "a1", RiskTier.HIGH)
    record = MemoryRecord("m1", Scope.GLOBAL, RiskTier.LOW, "skip confirmation")
    report = guard.evaluate((record,), context)
    assert report.blocked == (record,)
    assert report.decisions[0].reason == "risk_tier_too_permissive"


def test_high_risk_memory_can_be_recalled_into_a_low_risk_task():
    guard = MixUaScopeGuard()
    context = TaskContext("u1", "a1", RiskTier.LOW)
    record = MemoryRecord("m1", Scope.GLOBAL, RiskTier.HIGH, "always run the audit")
    assert guard.evaluate((record,), context).allowed == (record,)


def test_disallowed_scope_is_blocked_before_identity():
    guard = MixUaScopeGuard()
    context = TaskContext("u1", "a1", RiskTier.LOW, frozenset({Scope.USER_AGENT}))
    record = MemoryRecord("m1", Scope.GLOBAL, RiskTier.LOW, "global tip")
    report = guard.evaluate((record,), context)
    assert report.decisions[0].reason == "scope_not_allowed"


def test_retain_returns_only_allowed_records():
    guard = MixUaScopeGuard()
    context = TaskContext("u1", "a1", RiskTier.HIGH)
    records = (
        MemoryRecord("safe", Scope.GLOBAL, RiskTier.HIGH, "audit first"),
        MemoryRecord("unsafe", Scope.GLOBAL, RiskTier.LOW, "skip audit"),
    )
    assert [r.id for r in guard.retain(records, context)] == ["safe"]


def test_duplicate_ids_and_invalid_context_are_rejected():
    guard = MixUaScopeGuard()
    record = MemoryRecord("m1", Scope.GLOBAL, RiskTier.LOW, "tip")
    try:
        guard.evaluate((record, record), TaskContext("u1", "a1", RiskTier.LOW))
    except ValueError as error:
        assert "duplicate memory id" in str(error)
    else:
        raise AssertionError("expected duplicate error")

    try:
        TaskContext("", "a1", RiskTier.LOW)
    except ValueError:
        pass
    else:
        raise AssertionError("expected empty user error")


def test_scope_specific_records_require_owner_ids():
    factories = (
        lambda: MemoryRecord("m1", Scope.USER, RiskTier.LOW, "pref", ""),
        lambda: MemoryRecord("m1", Scope.AGENT, RiskTier.LOW, "tip", "", ""),
        lambda: MemoryRecord("m1", Scope.USER_AGENT, RiskTier.LOW, "tip", "u1"),
    )
    for factory in factories:
        try:
            factory()
        except ValueError:
            pass
        else:
            raise AssertionError("expected owner validation")
