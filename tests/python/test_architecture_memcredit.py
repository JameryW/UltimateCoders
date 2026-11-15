"""Tests for MemCredit marginal-utility arbitration and the directory trie."""

from __future__ import annotations

import pytest
from ultimate_coders.architecture.memcredit import (
    ATTEMPT_ONE_BUDGET,
    RETRY_BUDGET,
    DirectoryTrie,
    MemCreditCandidate,
    budget_for_attempt,
    directory_affinity,
    estimate_tokens,
    memcredit_enabled,
    select_memcredit,
    token_jaccard,
)


def test_budget_constants_and_attempt_budget() -> None:
    assert ATTEMPT_ONE_BUDGET == 800
    assert RETRY_BUDGET == 3000
    assert budget_for_attempt(0) == 800
    assert budget_for_attempt(-2) == 800
    assert budget_for_attempt(1) == 3000
    assert budget_for_attempt(9) == 3000


def test_estimate_tokens_rounds_up_and_never_returns_zero() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("a") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("x" * 8) == 2
    assert estimate_tokens("x" * 9) == 3
    assert MemCreditCandidate("id", "x" * 9).tokens == 3
    assert MemCreditCandidate("id", "hello", tokens=12).tokens == 12


def test_token_jaccard_is_case_insensitive_and_bounded() -> None:
    assert token_jaccard("Alpha beta", "alpha BETA") == 1.0
    assert token_jaccard("alpha beta", "gamma delta") == 0.0
    assert token_jaccard("alpha beta gamma", "beta gamma delta") == pytest.approx(0.5)
    assert token_jaccard("", "alpha") == 0.0
    assert token_jaccard("hello-world", "hello world") == 1.0


def test_directory_affinity_prefers_shared_subtree() -> None:
    assert directory_affinity("pkg/a/one.py", "pkg/a/two.py") == 1.5
    assert directory_affinity("pkg/one/a.py", "pkg/two/b.py") == 1.5
    assert directory_affinity("pkg\\a\\one.py", "pkg/a/two.py") == 1.5
    assert directory_affinity("root_a.py", "root_b.py") == 1.0
    assert directory_affinity("pkg/a/one.py", "tests/b/two.py") == 1.0
    assert directory_affinity("", "pkg/a/one.py") == 1.0


def test_select_uses_attempt_budget_by_default() -> None:
    huge = MemCreditCandidate("huge", "alpha beta", tokens=900)
    first = select_memcredit([huge], attempt=0)
    assert first.budget == 800
    assert first.selected == ()
    assert [item.reason for item in first.skipped] == ["budget"]

    retry = select_memcredit([huge], attempt=1)
    assert retry.budget == 3000
    assert [candidate.identifier for candidate in retry.selected] == ["huge"]
    assert retry.tokens_used == 900


def test_explicit_budget_overrides_attempt() -> None:
    candidate = MemCreditCandidate("small", "alpha", tokens=5, score=1.0)
    result = select_memcredit([candidate], budget_tokens=5, attempt=4)
    assert result.budget == 5
    assert [item.identifier for item in result.selected] == ["small"]


def test_density_orders_by_utility_per_token() -> None:
    cheap = MemCreditCandidate("cheap", "alpha", tokens=10, score=1.0)
    pricey = MemCreditCandidate("pricey", "beta", tokens=40, score=1.0)
    result = select_memcredit([pricey, cheap])
    assert [candidate.identifier for candidate in result.selected] == ["cheap", "pricey"]
    assert result.tokens_used == 50


def test_stage_bonus_breaks_utility_ties() -> None:
    ast = MemCreditCandidate("ast", "alpha", tokens=10, stage="ast")
    qdrant = MemCreditCandidate("qdrant", "beta", tokens=10, stage="qdrant")
    text = MemCreditCandidate("text", "gamma", tokens=10, stage="text")
    result = select_memcredit([text, qdrant, ast])
    assert [candidate.identifier for candidate in result.selected] == [
        "ast",
        "qdrant",
        "text",
    ]


def test_negative_scores_are_clamped_to_zero() -> None:
    boosted = MemCreditCandidate("boosted", "alpha", tokens=10, score=-4.0, stage="ast")
    plain = MemCreditCandidate("plain", "beta", tokens=10, score=-4.0)
    result = select_memcredit([plain, boosted])
    assert [candidate.identifier for candidate in result.selected] == ["boosted", "plain"]


def test_overlap_duplicate_and_empty_candidates_are_skipped() -> None:
    first = MemCreditCandidate("first", "alpha beta gamma")
    overlap = MemCreditCandidate("overlap", "alpha beta gamma")
    duplicate = MemCreditCandidate("first", "delta epsilon")
    empty = MemCreditCandidate("empty", "   ")
    result = select_memcredit([first, overlap, duplicate, empty])
    assert [candidate.identifier for candidate in result.selected] == ["first"]
    reasons = {item.identifier: item.reason for item in result.skipped}
    assert reasons == {"overlap": "overlap", "first": "duplicate", "empty": "empty"}


def test_candidate_that_does_not_fit_budget_is_skipped() -> None:
    first = MemCreditCandidate("a", "alpha", tokens=6, score=2.0)
    second = MemCreditCandidate("b", "beta", tokens=6, score=1.0)
    result = select_memcredit([first, second], budget_tokens=10)
    assert [candidate.identifier for candidate in result.selected] == ["a"]
    assert result.tokens_used == 6
    assert result.skipped[0].identifier == "b"
    assert result.skipped[0].reason == "budget"


def test_subtree_weight_boosts_directory_siblings() -> None:
    one = MemCreditCandidate("one", "alpha", path="pkg/a/one.py", tokens=10, score=1.0)
    two = MemCreditCandidate("two", "beta", path="pkg/a/two.py", tokens=20, score=1.0)
    three = MemCreditCandidate("three", "gamma", path="other/b/three.py", tokens=15, score=1.0)
    boosted = select_memcredit([one, two, three], budget_tokens=100)
    assert [candidate.identifier for candidate in boosted.selected] == ["one", "two", "three"]

    plain = select_memcredit([one, two, three], budget_tokens=100, subtree_weight=1.0)
    assert [candidate.identifier for candidate in plain.selected] == ["one", "three", "two"]


def test_mapping_candidates_are_accepted() -> None:
    result = select_memcredit(
        [
            {"identifier": "m1", "content": "alpha", "score": 1.0, "tokens": 5},
            {"identifier": "m2", "content": "beta", "score": 0.5, "tokens": 5},
        ]
    )
    assert result.budget == 800
    assert [candidate.identifier for candidate in result.selected] == ["m1", "m2"]


def test_directory_trie_counts_prefixes() -> None:
    trie = DirectoryTrie()
    trie.add("pkg/a/one.py")
    trie.add("pkg/a/two.py")
    trie.add("pkg/b/three.py")
    trie.add("root.py")
    trie.add("pkg/a/one.py")  # idempotent
    assert len(trie) == 4
    assert trie.prefix_count("") == 4
    assert trie.prefix_count("pkg") == 3
    assert trie.prefix_count("pkg/a") == 2
    assert trie.prefix_count("pkg\\a") == 2
    assert trie.prefix_count("pkg/a/one.py") == 1
    assert trie.prefix_count("missing") == 0


def test_directory_trie_best_prefix_and_affinity() -> None:
    trie = DirectoryTrie()
    trie.add("pkg/a/one.py")
    trie.add("pkg/a/two.py")
    trie.add("pkg/b/three.py")
    trie.add("root.py")
    assert trie.best_prefix("pkg/a/new.py") == "pkg/a"
    assert trie.best_prefix("pkg/b/new.py") == "pkg/b"
    assert trie.best_prefix("pkg/c/new.py") == "pkg"
    assert trie.best_prefix("root.py") == ""
    assert trie.best_prefix("other/new.py") == ""
    assert trie.subtree_affinity("pkg/a/new.py") == 1.5
    assert trie.subtree_affinity("pkg/b/new.py") == 1.5
    assert trie.subtree_affinity("other/new.py") == 1.0
    assert trie.subtree_affinity("") == 1.0


def test_directory_trie_indexes_directory_paths() -> None:
    trie = DirectoryTrie()
    trie.add("pkg/a/")
    trie.add("pkg/a/one.py")
    assert trie.prefix_count("pkg/a") == 2
    assert trie.best_prefix("pkg/a/one.py") == "pkg/a"

    root_trie = DirectoryTrie()
    root_trie.add("root.py")
    assert root_trie.subtree_affinity("other.py") == 1.0


def test_memcredit_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_MEMCREDIT", raising=False)
    assert memcredit_enabled() is False
    for value in ("1", "true", "YES", " on "):
        monkeypatch.setenv("UC_MEMCREDIT", value)
        assert memcredit_enabled() is True
    monkeypatch.setenv("UC_MEMCREDIT", "0")
    assert memcredit_enabled() is False
    monkeypatch.setenv("UC_MEMCREDIT", "false")
    assert memcredit_enabled() is False
