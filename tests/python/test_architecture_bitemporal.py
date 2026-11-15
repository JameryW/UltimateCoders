"""Tests for the DISCERN-style bitemporal claim graph."""

from __future__ import annotations

import pytest
from ultimate_coders.architecture.bitemporal import (
    BitemporalClaim,
    BitemporalGraph,
    EpistemicStatus,
    bitemporal_enabled,
)


def test_epistemic_status_values_and_membership() -> None:
    assert EpistemicStatus.UNVERIFIED_CLAIM.value == "unverified_claim"
    assert EpistemicStatus.ORACLE_VERIFIED == "oracle_verified"
    assert EpistemicStatus("negative_pitfall") is EpistemicStatus.NEGATIVE_PITFALL


def test_claim_defaults_and_dict_round_trip() -> None:
    claim = BitemporalClaim("c1", "cache is warm")
    assert claim.status is EpistemicStatus.UNVERIFIED_CLAIM
    assert claim.confidence == 0.5
    assert claim.evidence_ids == ()
    assert claim.expired_at_commit is None
    assert claim.superseded_by is None
    assert claim.is_live is True
    payload = claim.to_dict()
    assert payload == {
        "claim_id": "c1",
        "statement": "cache is warm",
        "status": "unverified_claim",
        "valid_from_commit": "",
        "expired_at_commit": None,
        "confidence": 0.5,
        "evidence_ids": [],
        "superseded_by": None,
        "metadata": {},
    }
    assert BitemporalClaim.from_dict(payload) == claim


def test_claim_coerces_hostile_input() -> None:
    claim = BitemporalClaim(
        "  c2  ",
        "statement",
        status="ORACLE_VERIFIED",
        expired_at_commit="",
        confidence=7.5,
        evidence_ids=("e1", "e1", " e2 "),
        superseded_by="   ",
        metadata={f"key{index}": index for index in range(25)},
    )
    assert claim.claim_id == "c2"
    assert claim.status is EpistemicStatus.ORACLE_VERIFIED
    assert claim.expired_at_commit is None
    assert claim.superseded_by is None
    assert claim.confidence == 1.0
    assert claim.evidence_ids == ("e1", "e2")
    assert len(claim.metadata) == 20

    fallback = BitemporalClaim("c3", "s", status="nonsense", confidence=float("nan"))
    assert fallback.status is EpistemicStatus.UNVERIFIED_CLAIM
    assert fallback.confidence == 0.5
    assert BitemporalClaim("c4", "s", confidence=-3.0).confidence == 0.0


def test_claim_from_dict_ignores_unknown_keys() -> None:
    claim = BitemporalClaim.from_dict(
        {"claim_id": "x", "statement": "y", "unknown": 1}
    )
    assert claim.claim_id == "x"
    assert claim.statement == "y"
    assert claim.status is EpistemicStatus.UNVERIFIED_CLAIM


def test_graph_add_is_idempotent_and_rejects_conflicts() -> None:
    graph = BitemporalGraph()
    original = BitemporalClaim("c1", "first")
    assert graph.add(original) is original
    assert graph.add(BitemporalClaim("c1", "first")) is original
    assert len(graph) == 1
    assert "c1" in graph
    with pytest.raises(ValueError):
        graph.add(BitemporalClaim("c1", "different"))
    with pytest.raises(ValueError):
        graph.add(BitemporalClaim("", "no id"))
    assert len(graph) == 1


def test_graph_add_accepts_mappings_and_rejects_bad_types() -> None:
    graph = BitemporalGraph()
    claim = graph.add(
        {"claim_id": "m1", "statement": "mapped", "status": "oracle_verified"}
    )
    assert isinstance(claim, BitemporalClaim)
    assert claim.status is EpistemicStatus.ORACLE_VERIFIED
    assert graph.get("m1") == claim
    assert graph.get("missing") is None
    with pytest.raises(TypeError):
        graph.add(5)


def test_expire_is_soft_and_retains_original() -> None:
    graph = BitemporalGraph()
    claim = graph.add(BitemporalClaim("c1", "cache is warm", valid_from_commit="c0"))
    updated = graph.expire("c1", at_commit="c9", superseded_by="c2")
    assert updated.expired_at_commit == "c9"
    assert updated.superseded_by == "c2"
    assert updated.is_live is False
    assert claim.is_live is True
    assert claim.expired_at_commit is None
    assert graph.get("c1") is updated
    assert graph.all() == [updated]
    with pytest.raises(KeyError):
        graph.expire("missing", at_commit="c9")
    with pytest.raises(ValueError):
        graph.expire("c1", at_commit="   ")


def test_expire_preserves_existing_superseded_by() -> None:
    graph = BitemporalGraph()
    graph.add(BitemporalClaim("d", "delta", superseded_by="e"))
    updated = graph.expire("d", at_commit="c3")
    assert updated.expired_at_commit == "c3"
    assert updated.superseded_by == "e"


def test_set_status_merges_evidence_and_keeps_original() -> None:
    graph = BitemporalGraph()
    claim = graph.add(BitemporalClaim("c1", "cache", evidence_ids=("e1",)))
    updated = graph.set_status(
        "c1", EpistemicStatus.ORACLE_VERIFIED, evidence_ids=("e2", "e1")
    )
    assert updated.status is EpistemicStatus.ORACLE_VERIFIED
    assert updated.evidence_ids == ("e1", "e2")
    assert claim.status is EpistemicStatus.UNVERIFIED_CLAIM
    with pytest.raises(KeyError):
        graph.set_status("missing", EpistemicStatus.ORACLE_VERIFIED)


def test_live_excludes_expired_and_superseded_claims() -> None:
    graph = BitemporalGraph()
    graph.add(BitemporalClaim("live", "live claim"))
    graph.add(BitemporalClaim("expired", "expired claim"))
    graph.add(BitemporalClaim("superseded", "superseded claim", superseded_by="live"))
    graph.expire("expired", at_commit="c1")
    assert [claim.claim_id for claim in graph.live()] == ["live"]
    assert len(graph.all()) == 3


def test_as_of_is_point_in_time() -> None:
    graph = BitemporalGraph()
    graph.add(BitemporalClaim("always", "always visible"))
    graph.add(BitemporalClaim("late", "valid from c1", valid_from_commit="c1"))
    graph.add(
        BitemporalClaim(
            "gone",
            "expired at c2",
            valid_from_commit="c1",
            expired_at_commit="c2",
        )
    )
    graph.add(
        BitemporalClaim(
            "superseded",
            "superseded",
            valid_from_commit="c1",
            superseded_by="late",
        )
    )

    def ids(commit: str, ancestors: list[str] | None = None) -> list[str]:
        return [claim.claim_id for claim in graph.as_of(commit, ancestors)]

    assert ids("c1", ["c1"]) == ["always", "gone", "late"]
    assert ids("c2", ["c1"]) == ["always", "late"]
    assert ids("c2", "c1") == ["always", "late"]
    assert ids("c3", ["c1", "c2"]) == ["always", "late"]
    assert ids("c2") == ["always"]
    assert len(graph.all()) == 4


def test_as_of_sorts_by_valid_from_then_claim_id() -> None:
    graph = BitemporalGraph()
    graph.add(BitemporalClaim("zeta", "z", valid_from_commit="c2"))
    graph.add(BitemporalClaim("beta", "b", valid_from_commit="c2"))
    graph.add(BitemporalClaim("alpha", "a", valid_from_commit="c1"))
    graph.add(BitemporalClaim("root", "r"))
    assert [claim.claim_id for claim in graph.as_of("c3", ["c1", "c2"])] == [
        "root",
        "alpha",
        "beta",
        "zeta",
    ]


def test_resolve_prefers_oracle_verified_then_confidence() -> None:
    graph = BitemporalGraph()
    graph.add(
        BitemporalClaim("unverified", "cache is warm", confidence=0.9, valid_from_commit="c1")
    )
    graph.add(
        BitemporalClaim(
            "oracle",
            "cache is warm",
            status=EpistemicStatus.ORACLE_VERIFIED,
            confidence=0.4,
            valid_from_commit="c1",
        )
    )
    graph.add(
        BitemporalClaim(
            "pitfall",
            "cache is warm",
            status=EpistemicStatus.NEGATIVE_PITFALL,
            confidence=1.0,
            valid_from_commit="c1",
        )
    )
    graph.add(BitemporalClaim("other", "cache is cold", valid_from_commit="c1"))
    graph.add(
        BitemporalClaim(
            "stale",
            "cache is warm",
            status=EpistemicStatus.ORACLE_VERIFIED,
            valid_from_commit="c1",
            expired_at_commit="c2",
        )
    )
    assert [claim.claim_id for claim in graph.resolve("c3", ["c1", "c2"])] == [
        "oracle",
        "other",
    ]


def test_resolve_tiebreaks_confidence_recency_and_id() -> None:
    confidence = BitemporalGraph()
    confidence.add(BitemporalClaim("weak", "same truth", confidence=0.2, valid_from_commit="c1"))
    confidence.add(BitemporalClaim("strong", "same truth", confidence=0.8, valid_from_commit="c1"))
    assert [claim.claim_id for claim in confidence.resolve("c2", ["c1"])] == ["strong"]

    recency = BitemporalGraph()
    recency.add(BitemporalClaim("old", "same truth", valid_from_commit="c1"))
    recency.add(BitemporalClaim("new", "same truth", valid_from_commit="c2"))
    assert [claim.claim_id for claim in recency.resolve("c3", ["c1", "c2"])] == ["new"]

    tie = BitemporalGraph()
    tie.add(BitemporalClaim("cv1", "same truth", valid_from_commit="c1"))
    tie.add(BitemporalClaim("cv2", "same truth", valid_from_commit="c1"))
    assert [claim.claim_id for claim in tie.resolve("c2", ["c1"])] == ["cv2"]


def test_resolve_collapses_whitespace_and_never_mutates() -> None:
    graph = BitemporalGraph()
    graph.add(
        BitemporalClaim("spaced", "cache   is\n warm", confidence=0.2, valid_from_commit="c1")
    )
    graph.add(BitemporalClaim("plain", "cache is warm", confidence=0.9, valid_from_commit="c1"))
    before = graph.all()
    resolved = graph.resolve("c2", ["c1"])
    assert [claim.claim_id for claim in resolved] == ["plain"]
    assert graph.all() == before
    assert graph.get("spaced").statement == "cache   is\n warm"


def test_graph_dict_round_trip() -> None:
    graph = BitemporalGraph()
    graph.add(
        BitemporalClaim(
            "a",
            "alpha",
            status=EpistemicStatus.ORACLE_VERIFIED,
            valid_from_commit="c1",
            confidence=0.8,
        )
    )
    graph.add(BitemporalClaim("b", "beta", expired_at_commit="c2", metadata={"k": "v"}))
    payload = graph.to_dicts()
    restored = BitemporalGraph.from_dicts(payload)
    assert restored.to_dicts() == payload
    assert BitemporalGraph.from_dicts(None).to_dicts() == []
    assert len(BitemporalGraph.from_dicts([payload[0], None, "junk"]).all()) == 1


def test_bitemporal_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_BITEMPORAL_KNOWLEDGE", raising=False)
    assert bitemporal_enabled() is False
    monkeypatch.setenv("UC_BITEMPORAL_KNOWLEDGE", "true")
    assert bitemporal_enabled() is True
    monkeypatch.setenv("UC_BITEMPORAL_KNOWLEDGE", "off")
    assert bitemporal_enabled() is False
