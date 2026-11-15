"""Tests for the local complementary adapter behavior router."""

from __future__ import annotations

import pytest
from ultimate_coders.architecture.behavior_router import (
    POSTGRES_DDL,
    AdapterBehaviorProfile,
    AdapterChoice,
    BehaviorProfileStore,
    ComplementaryAdapterRouter,
    LocalTfidfAdapterIndex,
    behavior_routing_enabled,
    is_complementary,
    postgres_ddl,
    tokenize,
)


def _profile(adapter: str, **overrides: object) -> AdapterBehaviorProfile:
    payload: dict[str, object] = {
        "adapter": adapter,
        "wins": 1,
        "losses": 1,
        "failures": 0,
        "avg_tokens": 0.0,
        "avg_latency_ms": 0.0,
        "failure_kinds": (),
        "keywords": (),
        "metadata": {},
    }
    payload.update(overrides)
    return AdapterBehaviorProfile(**payload)  # type: ignore[arg-type]


def test_tokenize_lowercases_and_drops_single_characters() -> None:
    assert tokenize("Async Timeout: 500 on db_conn (x)") == (
        "async",
        "timeout",
        "500",
        "on",
        "db_conn",
    )
    assert tokenize("Under_score-X") == ("under_score",)
    assert tokenize("") == ()
    assert tokenize("!!!") == ()


def test_win_rate_is_neutral_without_decisions() -> None:
    assert _profile("a", wins=0, losses=0).win_rate == 0.5
    assert _profile("a", wins=3, losses=1).win_rate == 0.75
    assert _profile("a", wins=0, losses=4).win_rate == 0.0


def test_with_outcome_updates_running_averages_and_failure_kinds() -> None:
    profile = AdapterBehaviorProfile(adapter="alpha")
    first = profile.with_outcome("alpha", won=True, tokens=100.0, latency_ms=10.0)
    assert first.wins == 1
    assert first.losses == 0
    assert first.avg_tokens == 100.0
    assert first.avg_latency_ms == 10.0
    assert profile.wins == 0  # source profile is untouched

    second = first.with_outcome(
        "alpha", won=False, tokens=200.0, latency_ms=30.0, failure_kind="timeout"
    )
    assert (second.wins, second.losses) == (1, 1)
    assert second.avg_tokens == 150.0
    assert second.avg_latency_ms == 20.0
    assert second.failure_kinds == ("timeout",)
    assert second.win_rate == 0.5

    third = second.with_outcome("alpha", won=False, failure_kind="rate_limit")
    assert (third.wins, third.losses) == (1, 2)
    assert third.failure_kinds == ("timeout", "rate_limit")


def test_profile_dict_round_trip() -> None:
    profile = AdapterBehaviorProfile(
        adapter="beta",
        wins=2,
        losses=1,
        failures=3,
        avg_tokens=12.5,
        avg_latency_ms=4.0,
        failure_kinds=("timeout",),
        keywords=("slow", "timeout"),
        metadata={"tier": "fallback"},
    )
    row = profile.to_dict()
    assert row["adapter"] == "beta"
    assert row["failure_kinds"] == ["timeout"]
    assert row["keywords"] == ["slow", "timeout"]
    assert AdapterBehaviorProfile.from_dict(row) == profile


def test_store_upsert_get_and_sorted_all() -> None:
    store = BehaviorProfileStore()
    store.upsert(_profile("beta", wins=2))
    store.upsert(_profile("alpha"))
    assert store.get("beta") is not None
    assert store.get("beta").wins == 2  # type: ignore[union-attr]
    assert store.get("missing") is None
    assert [profile.adapter for profile in store.all()] == ["alpha", "beta"]
    assert len(store) == 2


def test_store_record_outcome_creates_then_accumulates() -> None:
    store = BehaviorProfileStore()
    created = store.record_outcome(
        "gamma", won=True, tokens=40.0, latency_ms=8.0
    )
    assert created.adapter == "gamma"
    assert created.wins == 1
    stored = store.get("gamma")
    assert stored is not None and stored.wins == 1

    updated = store.record_outcome(
        "gamma", won=False, tokens=60.0, latency_ms=12.0, failure_kind="rate_limit"
    )
    assert (updated.wins, updated.losses) == (1, 1)
    assert updated.avg_tokens == 50.0
    assert updated.avg_latency_ms == 10.0
    assert updated.failure_kinds == ("rate_limit",)
    assert store.get("gamma") == updated  # type: ignore[arg-type]


def test_store_load_rows_and_to_rows_round_trip() -> None:
    rows = [
        {
            "adapter": "alpha",
            "wins": 3,
            "losses": 1,
            "failures": 2,
            "avg_tokens": 123.5,
            "avg_latency_ms": 45.0,
            "failure_kinds": ["timeout"],
            "keywords": ["fast", "timeout"],
        },
        {"adapter": "beta"},
    ]
    store = BehaviorProfileStore()
    store.load_rows(rows)
    alpha = store.get("alpha")
    assert alpha is not None
    assert alpha.wins == 3
    assert alpha.failure_kinds == ("timeout",)
    assert alpha.keywords == ("fast", "timeout")
    beta = store.get("beta")
    assert beta is not None and beta.win_rate == 0.5

    exported = store.to_rows()
    assert [row["adapter"] for row in exported] == ["alpha", "beta"]
    assert exported[0]["failure_kinds"] == ["timeout"]

    reloaded = BehaviorProfileStore()
    reloaded.load_rows(exported)
    assert [profile.adapter for profile in reloaded.all()] == ["alpha", "beta"]
    assert reloaded.get("alpha") == alpha


def test_postgres_ddl_is_exposed() -> None:
    ddl = postgres_ddl()
    assert ddl == POSTGRES_DDL
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS adapter_behavior_profiles")
    assert "TEXT[]" in ddl
    assert "TIMESTAMPTZ" in ddl


def test_tfidf_similarity_uses_keywords_and_failure_kinds() -> None:
    index = LocalTfidfAdapterIndex(
        [
            _profile(
                "flaky",
                failure_kinds=("timeout",),
                keywords=("timeout", "database"),
            ),
            _profile("layout", keywords=("frontend", "css")),
        ]
    )
    assert index.similarity("database timeout", "flaky") > 0.5
    assert index.similarity("database timeout", "layout") == 0.0


def test_tfidf_rank_orders_by_score_and_breaks_ties_by_name() -> None:
    index = LocalTfidfAdapterIndex(
        [
            _profile("slow", keywords=("layout",)),
            _profile("fast", keywords=("timeout",)),
        ]
    )
    ranked = index.rank("timeout failure")
    assert [adapter for adapter, _score in ranked] == ["fast", "slow"]
    assert ranked[0][1] > ranked[1][1]

    tied = LocalTfidfAdapterIndex(
        [
            _profile("bb", keywords=("timeout",)),
            _profile("aa", keywords=("timeout",)),
        ]
    )
    tie_ranked = tied.rank("timeout")
    assert [adapter for adapter, _score in tie_ranked] == ["aa", "bb"]
    assert tie_ranked[0][1] == tie_ranked[1][1]


def test_is_complementary_jaccard_threshold() -> None:
    profile = _profile("a", failure_kinds=("timeout", "rate_limit"))
    assert is_complementary(profile, "database migration") is True
    assert is_complementary(profile, "") is True
    assert is_complementary(profile, "timeout") is False
    assert is_complementary(profile, "timeout rate limit errors") is True
    assert is_complementary(_profile("b"), "timeout") is True


def test_router_excludes_failed_adapter_when_alternatives_exist() -> None:
    store = BehaviorProfileStore()
    for adapter in ("alpha", "beta", "gamma"):
        store.upsert(_profile(adapter, wins=2, losses=1))
    router = ComplementaryAdapterRouter(store)
    choice = router.choose("please route this task", failed_adapter="alpha")
    assert choice is not None
    assert choice.adapter in {"beta", "gamma"}
    assert "alpha" not in choice.alternatives
    assert set(choice.alternatives) <= {"beta", "gamma"}
    assert "failed_excluded" in choice.reason
    assert "tfidf=" in choice.reason
    assert "win_rate=" in choice.reason
    assert "complement=" in choice.reason


def test_router_falls_back_to_failed_adapter_when_only_alternative() -> None:
    store = BehaviorProfileStore()
    store.upsert(_profile("alpha"))
    router = ComplementaryAdapterRouter(store)
    choice = router.choose("task", failed_adapter="alpha")
    assert choice is not None
    assert choice.adapter == "alpha"


def test_router_none_without_candidates() -> None:
    router = ComplementaryAdapterRouter(BehaviorProfileStore())
    assert router.choose("task", failed_adapter="alpha") is None


def test_router_respects_explicit_candidates() -> None:
    store = BehaviorProfileStore()
    for adapter in ("alpha", "beta", "gamma"):
        store.upsert(_profile(adapter, keywords=(adapter,)))
    router = ComplementaryAdapterRouter(store)
    choice = router.choose("task", failed_adapter="alpha", candidates=["beta"])
    assert choice is not None
    assert choice.adapter == "beta"


def test_router_breaks_equal_scores_by_adapter_name() -> None:
    store = BehaviorProfileStore()
    store.upsert(_profile("bb"))
    store.upsert(_profile("aa"))
    router = ComplementaryAdapterRouter(store)
    choice = router.choose("", failed_adapter="missing")
    assert choice is not None
    assert choice.adapter == "aa"
    assert choice.alternatives == ("bb",)


def test_router_prefers_complementary_adapter_for_failure_text() -> None:
    store = BehaviorProfileStore()
    store.upsert(
        _profile(
            "retry_timeout",
            wins=1,
            losses=1,
            failure_kinds=("timeout",),
            keywords=("retry",),
        )
    )
    store.upsert(
        _profile(
            "retry_memory",
            wins=1,
            losses=1,
            failure_kinds=("memory",),
            keywords=("retry",),
        )
    )
    router = ComplementaryAdapterRouter(store)
    choice = router.choose(
        "please route",
        failed_adapter="broken",
        failure_kind="timeout",
        failure_text="timeout",
    )
    assert choice is not None
    assert choice.adapter == "retry_memory"


def test_adapter_choice_to_dict() -> None:
    choice = AdapterChoice(
        adapter="beta", score=0.5, reason="why", alternatives=("alpha",)
    )
    assert choice.to_dict() == {
        "adapter": "beta",
        "score": 0.5,
        "reason": "why",
        "alternatives": ["alpha"],
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("off", False),
        ("", False),
    ],
)
def test_behavior_routing_enabled_reads_env(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("UC_ADAPTIVE_ADAPTER_ROUTING", value)
    assert behavior_routing_enabled() is expected


def test_behavior_routing_enabled_defaults_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UC_ADAPTIVE_ADAPTER_ROUTING", raising=False)
    assert behavior_routing_enabled() is False
