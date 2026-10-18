"""Contract tests for the deterministic context compiler.

Pins the properties the worker relies on: tier ordering, budget honesty
(P0 is never silently dropped), deterministic output independent of input
order, TTL/duplicate handling, and rendering that matches the pre-compiler
injector for the dependency block.
"""

from __future__ import annotations

import random

from ultimate_coders.agent.context_compiler import (
    ContextItem,
    Tier,
    compile_context,
    estimate_tokens,
    from_dependency_results,
    from_search_block,
)
from ultimate_coders.agent.state_sync import ContextInjector


def _item(item_id: str, tier: Tier, content: str, **kwargs) -> ContextItem:
    return ContextItem(item_id=item_id, tier=tier, content=content, **kwargs)


class _Ctx:
    """Duck-typed stand-in for state_sync.SubtaskContext."""

    def __init__(
        self, subtask_id, summary="", modified_files=None, key_findings=None, success=True
    ):
        self.subtask_id = subtask_id
        self.summary = summary
        self.modified_files = modified_files or []
        self.key_findings = key_findings or []
        self.success = success


# ── token estimate ──────────────────────────────────────────────────


def test_estimate_tokens_is_deterministic_and_never_zero():
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("x" * 400) == estimate_tokens("x" * 400)


# ── ordering and budget ─────────────────────────────────────────────


def test_render_orders_by_tier():
    manifest = compile_context(
        [
            _item("b", Tier.BACKGROUND, "bg"),
            _item("p", Tier.POLICY, "policy"),
            _item("s", Tier.STATE, "state"),
        ],
        budget_tokens=10_000,
    )
    text = manifest.render()
    assert text.index("policy") < text.index("state") < text.index("bg")
    assert "### P0 policy" in text and "### P5 background" in text


def test_p0_is_never_dropped_and_over_budget_is_reported():
    manifest = compile_context(
        [
            _item("contract", Tier.POLICY, "x" * 400),  # 100 tokens
            _item("bg", Tier.BACKGROUND, "y" * 400),
        ],
        budget_tokens=50,
    )
    assert [i.item_id for i in manifest.items] == ["contract"]
    assert manifest.over_budget is True
    assert manifest.dropped and manifest.dropped[0].item_id == "bg"
    assert manifest.dropped[0].reason == "budget"


def test_budget_packs_lower_tiers_only_while_they_fit():
    manifest = compile_context(
        [
            _item("big", Tier.EVIDENCE, "a" * 400),  # 100 tokens
            _item("small", Tier.EVIDENCE, "b" * 40),  # 10 tokens
            _item("bg", Tier.BACKGROUND, "c" * 40),
        ],
        budget_tokens=60,
    )
    ids = [i.item_id for i in manifest.items]
    # big is skipped, small still fits, background takes what is left.
    assert ids == ["small", "bg"]
    assert {d.item_id: d.reason for d in manifest.dropped} == {"big": "budget"}


def test_same_tier_orders_verified_then_relevance_then_id():
    manifest = compile_context(
        [
            _item("z", Tier.EVIDENCE, "z", relevance=0.1),
            _item("u", Tier.EVIDENCE, "u", relevance=0.9),
            _item("v", Tier.EVIDENCE, "v", relevance=0.5, verified=True),
            _item("a", Tier.EVIDENCE, "a", relevance=0.5),
        ],
        budget_tokens=10_000,
    )
    assert [i.item_id for i in manifest.items] == ["v", "u", "a", "z"]


def test_compilation_is_independent_of_input_order():
    items = [_item(f"i{n}", Tier.EVIDENCE, f"content-{n}", relevance=n / 10) for n in range(8)]
    shuffled = items[:]
    random.Random(7).shuffle(shuffled)
    a = compile_context(items, budget_tokens=10_000)
    b = compile_context(shuffled, budget_tokens=10_000)
    assert [i.item_id for i in a.items] == [i.item_id for i in b.items]
    assert a.render() == b.render()


# ── ttl and duplicates ─────────────────────────────────────────────


def test_expired_items_are_dropped_with_reason():
    now = 1_000.0
    manifest = compile_context(
        [
            _item("fresh", Tier.STATE, "fresh", observed_at=now - 10, ttl_seconds=60),
            _item("stale", Tier.STATE, "stale", observed_at=now - 10, ttl_seconds=5),
        ],
        budget_tokens=10_000,
        now=now,
    )
    assert [i.item_id for i in manifest.items] == ["fresh"]
    assert {d.item_id: d.reason for d in manifest.dropped} == {"stale": "expired"}


def test_duplicate_content_keeps_the_most_trusted_copy():
    manifest = compile_context(
        [
            _item("trace", Tier.TRACE, "same fact", relevance=1.0),
            _item("evidence", Tier.EVIDENCE, "same fact", verified=True),
            _item("bg", Tier.BACKGROUND, "same fact"),
        ],
        budget_tokens=10_000,
    )
    assert [i.item_id for i in manifest.items] == ["evidence"]
    assert {d.item_id: d.reason for d in manifest.dropped} == {
        "trace": "duplicate",
        "bg": "duplicate",
    }


# ── rendering ──────────────────────────────────────────────────────


def test_render_parts_splits_stable_prefix_from_volatile_tail():
    manifest = compile_context(
        [
            _item("state", Tier.STATE, "current step 2"),
            _item("policy", Tier.POLICY, "do not touch main", stable=True),
        ],
        budget_tokens=10_000,
    )
    prefix, tail = manifest.render_parts()
    assert "do not touch main" in prefix
    assert "current step 2" in tail
    assert "current step 2" not in prefix


def test_render_legacy_matches_the_injector_for_dependency_results():
    contexts = [
        _Ctx(
            "node-aaa-111",
            summary="implemented auth",
            modified_files=["login.py"],
            key_findings=["used jwt"],
        ),
        _Ctx("node-bbb-222", success=False),
    ]
    injector = ContextInjector()
    for ctx in contexts:
        injector.add_result(
            ctx.subtask_id,
            summary=ctx.summary,
            modified_files=ctx.modified_files,
            key_findings=ctx.key_findings,
            success=ctx.success,
        )
    expected = injector.build_context([c.subtask_id for c in contexts])

    manifest = compile_context(from_dependency_results(contexts), budget_tokens=10_000)
    assert manifest.render_legacy() == expected


def test_render_legacy_joins_the_search_block_like_the_worker_did():
    contexts = [_Ctx("node-ccc-333", summary="did work")]
    manifest = compile_context(
        from_dependency_results(contexts) + from_search_block("## Search results\nmain.rs:42"),
        budget_tokens=10_000,
    )
    text = manifest.render_legacy()
    assert text.endswith("\n\n## Search results\nmain.rs:42")


def test_render_legacy_marks_truncation_only_when_the_budget_dropped_something():
    contexts = [_Ctx("node-ddd-444", summary="s" * 4_000)]
    generous = compile_context(from_dependency_results(contexts), budget_tokens=100_000)
    assert "truncated by budget" not in generous.render_legacy()

    tight = compile_context(from_dependency_results(contexts), budget_tokens=50)
    assert "truncated by budget" in tight.render_legacy()


# ── observability ──────────────────────────────────────────────────


def test_summary_carries_counts_and_drops_but_no_content():
    manifest = compile_context(
        [
            _item("policy", Tier.POLICY, "secret policy text"),
            _item("bg", Tier.BACKGROUND, "x" * 400),
        ],
        budget_tokens=3,
        risk="high",
    )
    summary = manifest.summary()
    assert summary["risk"] == "high"
    assert summary["over_budget"] is True
    assert summary["items_by_tier"] == {"P0 policy": 1}
    assert summary["dropped"] == [
        {"item_id": "bg", "tier": int(Tier.BACKGROUND), "reason": "budget"}
    ]
    assert "secret policy text" not in repr(summary)


def test_dependency_adapter_marks_success_as_verified():
    items = from_dependency_results([_Ctx("n-1", summary="ok"), _Ctx("n-2", success=False)])
    assert [i.verified for i in items] == [True, False]
    assert [i.source_version for i in items] == ["ok", "failed"]


def test_empty_sources_compile_to_an_empty_manifest():
    manifest = compile_context([], budget_tokens=1_000)
    assert manifest.items == [] and manifest.total_tokens == 0
    assert manifest.render() == "" and manifest.render_legacy() == ""
