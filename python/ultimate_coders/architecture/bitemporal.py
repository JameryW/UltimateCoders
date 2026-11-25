"""DISCERN-style bitemporal claim graph with soft invalidation.

A claim is a statement about the repository carrying two time axes:

* when it became valid (``valid_from_commit``), and
* when it stopped being valid (``expired_at_commit``), which is recorded by
  :meth:`BitemporalGraph.expire` without deleting the original record.

Reads are point-in-time: :meth:`BitemporalGraph.as_of` answers "what did the
graph believe at this commit?" while :meth:`BitemporalGraph.resolve` collapses
duplicate statements by epistemic strength (oracle verified > unverified >
negative pitfall), confidence and recency. Expiry and status changes are soft:
the claim stays in the graph, but it is no longer live. Nothing is mutated in
place, so a caller may keep records from earlier reads.

Feature flag: ``UC_BITEMPORAL_KNOWLEDGE``.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

__all__ = [
    "BITEMPORAL_ENABLED_ENV",
    "BitemporalClaim",
    "BitemporalGraph",
    "EpistemicStatus",
    "bitemporal_enabled",
]

BITEMPORAL_ENABLED_ENV = "UC_BITEMPORAL_KNOWLEDGE"

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_METADATA_KEY_LIMIT = 20
_STATUS_PRIORITY = {
    "oracle_verified": 3,
    "unverified_claim": 2,
    "negative_pitfall": 1,
}


class EpistemicStatus(str, Enum):
    """How strongly a claim is supported by oracle evidence."""

    UNVERIFIED_CLAIM = "unverified_claim"
    ORACLE_VERIFIED = "oracle_verified"
    NEGATIVE_PITFALL = "negative_pitfall"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _coerce_status(value: Any) -> EpistemicStatus:
    if isinstance(value, EpistemicStatus):
        return value
    key = _text(value).strip().lower()
    for status in EpistemicStatus:
        if key in (status.value, status.name.lower()):
            return status
    return EpistemicStatus.UNVERIFIED_CLAIM


def _coerce_confidence(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.5
    if not math.isfinite(result):
        return 0.5
    return min(1.0, max(0.0, result))


def _bounded_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    bounded: dict[str, Any] = {}
    for key, item in value.items():
        if len(bounded) >= _METADATA_KEY_LIMIT:
            break
        bounded[str(key)] = item
    return bounded


def _coerce_evidence_ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items: Iterable[Any] = (value,)
    else:
        try:
            items = tuple(value or ())
        except TypeError:
            return ()
    result: list[str] = []
    for item in items:
        text = _text(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _commit_set(value: Iterable[str] | str | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        value = (value,)
    commits = set()
    for item in value:
        text = _text(item).strip()
        if text:
            commits.add(text)
    return frozenset(commits)


@dataclass(frozen=True)
class BitemporalClaim:
    """One immutable claim record with bitemporal validity boundaries."""

    claim_id: str
    statement: str
    status: EpistemicStatus = EpistemicStatus.UNVERIFIED_CLAIM
    valid_from_commit: str = ""
    expired_at_commit: str | None = None
    confidence: float = 0.5
    evidence_ids: tuple[str, ...] = ()
    superseded_by: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", _text(self.claim_id).strip())
        object.__setattr__(self, "statement", _text(self.statement))
        object.__setattr__(self, "status", _coerce_status(self.status))
        object.__setattr__(self, "valid_from_commit", _text(self.valid_from_commit).strip())
        expired = _text(self.expired_at_commit).strip() or None
        object.__setattr__(self, "expired_at_commit", expired)
        object.__setattr__(self, "confidence", _coerce_confidence(self.confidence))
        object.__setattr__(self, "evidence_ids", _coerce_evidence_ids(self.evidence_ids))
        successor = _text(self.superseded_by).strip() or None
        object.__setattr__(self, "superseded_by", successor)
        object.__setattr__(self, "metadata", _bounded_metadata(self.metadata))

    @property
    def is_live(self) -> bool:
        """Whether the claim is neither expired nor superseded."""

        return self.expired_at_commit is None and self.superseded_by is None

    def to_dict(self) -> dict:
        """JSON-safe projection; metadata is bounded to 20 keys."""

        return {
            "claim_id": self.claim_id,
            "statement": self.statement,
            "status": self.status.value,
            "valid_from_commit": self.valid_from_commit,
            "expired_at_commit": self.expired_at_commit,
            "confidence": float(self.confidence),
            "evidence_ids": list(self.evidence_ids),
            "superseded_by": self.superseded_by,
            "metadata": _bounded_metadata(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BitemporalClaim:
        """Rebuild a claim from :meth:`to_dict` output (unknown keys ignored)."""

        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            claim_id=_text(data.get("claim_id")),
            statement=_text(data.get("statement")),
            status=_coerce_status(data.get("status")),
            valid_from_commit=_text(data.get("valid_from_commit")),
            expired_at_commit=data.get("expired_at_commit"),
            confidence=data.get("confidence", 0.5),
            evidence_ids=data.get("evidence_ids") or (),
            superseded_by=data.get("superseded_by"),
            metadata=data.get("metadata"),
        )


class BitemporalGraph:
    """In-memory bitemporal graph with soft expiry and status upgrades."""

    def __init__(self) -> None:
        self._claims: dict[str, BitemporalClaim] = {}

    def __len__(self) -> int:
        return len(self._claims)

    def __contains__(self, claim_id: object) -> bool:
        return isinstance(claim_id, str) and claim_id.strip() in self._claims

    def _require(self, claim_id: str) -> BitemporalClaim:
        key = _text(claim_id).strip()
        try:
            return self._claims[key]
        except KeyError:
            raise KeyError(f"unknown claim_id: {key!r}") from None

    def add(self, claim: BitemporalClaim | Mapping[str, Any]) -> BitemporalClaim:
        """Insert one claim; identical re-adds are idempotent.

        A duplicate ``claim_id`` with different content is rejected with
        :class:`ValueError`; invalidation must go through :meth:`expire`,
        :meth:`set_status` or the ``superseded_by`` field.
        """

        if isinstance(claim, BitemporalClaim):
            record = claim
        elif isinstance(claim, Mapping):
            record = BitemporalClaim.from_dict(claim)
        else:
            raise TypeError("claim must be a BitemporalClaim or a mapping")
        if not record.claim_id:
            raise ValueError("claim_id is required")
        existing = self._claims.get(record.claim_id)
        if existing is not None:
            if existing == record:
                return existing
            raise ValueError(f"conflicting claim for id {record.claim_id!r}")
        self._claims[record.claim_id] = record
        return record

    def expire(
        self,
        claim_id: str,
        *,
        at_commit: str,
        superseded_by: str | None = None,
    ) -> BitemporalClaim:
        """Soft-expire a claim at ``at_commit``; the record is retained."""

        existing = self._require(claim_id)
        commit = _text(at_commit).strip()
        if not commit:
            raise ValueError("at_commit is required to expire a claim")
        successor = _text(superseded_by).strip() or None
        updated = replace(
            existing,
            expired_at_commit=existing.expired_at_commit or commit,
            superseded_by=successor if successor is not None else existing.superseded_by,
        )
        self._claims[existing.claim_id] = updated
        return updated

    def set_status(
        self,
        claim_id: str,
        status: EpistemicStatus | str,
        *,
        evidence_ids: Iterable[str] = (),
    ) -> BitemporalClaim:
        """Replace the epistemic status and merge in new evidence ids."""

        existing = self._require(claim_id)
        merged = existing.evidence_ids + _coerce_evidence_ids(evidence_ids)
        updated = replace(
            existing,
            status=_coerce_status(status),
            evidence_ids=merged,
        )
        self._claims[existing.claim_id] = updated
        return updated

    def get(self, claim_id: str) -> BitemporalClaim | None:
        return self._claims.get(_text(claim_id).strip())

    def all(self) -> list[BitemporalClaim]:
        """Every claim, insertion order, including expired/superseded ones."""

        return list(self._claims.values())

    def live(self) -> list[BitemporalClaim]:
        """Claims that are neither expired nor superseded, insertion order."""

        return [claim for claim in self._claims.values() if claim.is_live]

    def as_of(
        self,
        commit: str,
        known_ancestors: Iterable[str] | None = None,
    ) -> list[BitemporalClaim]:
        """Claims visible at ``commit``, oldest validity first.

        A claim is visible when it was introduced before the read point (empty
        ``valid_from_commit``, or ``valid_from_commit`` is one of the supplied
        ``known_ancestors``), it has not expired at the read point (the read
        commit is not its ``expired_at_commit`` and its expiry commit is not a
        known ancestor), and it is not superseded. Without ``known_ancestors``
        the read is conservative: only claims with an empty
        ``valid_from_commit`` qualify.
        """

        target = _text(commit).strip()
        ancestors = _commit_set(known_ancestors)
        visible = [
            claim
            for claim in self._claims.values()
            if _visible_as_of(claim, target, ancestors)
        ]
        visible.sort(key=lambda claim: (claim.valid_from_commit, claim.claim_id))
        return visible

    def resolve(
        self,
        commit: str,
        known_ancestors: Iterable[str] | None = None,
    ) -> list[BitemporalClaim]:
        """Collapse duplicate statements into the strongest live claim.

        Winners are ranked by status (oracle verified > unverified claim >
        negative pitfall), then confidence, then newer ``valid_from_commit``
        and finally ``claim_id`` for determinism. Statements are matched
        exactly (whitespace collapsed); there is no entity merging and the
        stored records are never mutated.
        """

        winners: dict[str, BitemporalClaim] = {}
        for claim in self.as_of(commit, known_ancestors):
            key = " ".join(claim.statement.split())
            current = winners.get(key)
            if current is None or _preference(claim) > _preference(current):
                winners[key] = claim
        resolved = list(winners.values())
        resolved.sort(key=lambda claim: (claim.valid_from_commit, claim.claim_id))
        return resolved

    def to_dicts(self) -> list[dict]:
        """Serialize every claim (insertion order) for persistence."""

        return [claim.to_dict() for claim in self._claims.values()]

    @classmethod
    def from_dicts(cls, payload: Iterable[Mapping[str, Any]] | None) -> BitemporalGraph:
        """Rebuild a graph from :meth:`to_dicts` output."""

        graph = cls()
        if payload is None:
            return graph
        try:
            items = tuple(payload)
        except TypeError:
            return graph
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if not _text(item.get("claim_id")).strip():
                continue
            graph.add(item)
        return graph


def _visible_as_of(
    claim: BitemporalClaim,
    commit: str,
    ancestors: frozenset[str],
) -> bool:
    if claim.valid_from_commit and claim.valid_from_commit not in ancestors:
        return False
    if claim.expired_at_commit is not None and (
        commit == claim.expired_at_commit or claim.expired_at_commit in ancestors
    ):
        return False
    return claim.superseded_by is None


def _preference(claim: BitemporalClaim) -> tuple[int, float, str, str]:
    return (
        _STATUS_PRIORITY.get(claim.status.value, 0),
        claim.confidence,
        claim.valid_from_commit,
        claim.claim_id,
    )


def bitemporal_enabled() -> bool:
    """Whether ``UC_BITEMPORAL_KNOWLEDGE`` enables the bitemporal graph."""

    value = os.environ.get(BITEMPORAL_ENABLED_ENV, "")
    return value.strip().lower() in _TRUTHY_VALUES
