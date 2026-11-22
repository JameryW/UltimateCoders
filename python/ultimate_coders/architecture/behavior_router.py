"""Local, zero-API complementary adapter routing.

V9 retries must not resend the same task to the same adapter, or to an adapter
whose observed failure kinds match the failure being recovered from.  This
module makes that decision entirely locally: no provider API, no network call
and no third-party dependency.

The router combines three cheap signals:

* TF-IDF cosine between the task text and an adapter's declared keywords,
  failure kinds and name (:class:`LocalTfidfAdapterIndex`);
* the adapter's historical win rate from :class:`BehaviorProfileStore`;
* a complement score, ``1 - keyword_similarity(profile.failure_kinds,
  failure_text)``, so an adapter that already failed the same way is routed
  away from whenever a complementary alternative exists.

The feature flag ``UC_ADAPTIVE_ADAPTER_ROUTING`` gates the caller, not the
classes: everything here is safe to import and construct while the flag is
unset.

Assumptions:

* a candidate without a stored profile is scored as a neutral fresh profile
  (win rate 0.5) instead of being dropped, so a cold store still routes;
* an empty/unknown ``failure_text`` means "no known incompatibility", so the
  complement component is 1.0;
* candidates whose failure kinds match the failure text (Jaccard >= 0.5) are
  filtered only while at least one complementary candidate remains; otherwise
  the whole pool is scored so a choice can still be returned.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "POSTGRES_DDL",
    "AdapterBehaviorProfile",
    "AdapterChoice",
    "BehaviorProfileStore",
    "ComplementaryAdapterRouter",
    "LocalTfidfAdapterIndex",
    "behavior_routing_enabled",
    "is_complementary",
    "keyword_similarity",
    "postgres_ddl",
    "tokenize",
]

#: PostgreSQL DDL for the durable mirror of the in-memory profile store.
POSTGRES_DDL = (
    "CREATE TABLE IF NOT EXISTS adapter_behavior_profiles (adapter TEXT PRIMARY KEY, "
    "wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0, "
    "failures INTEGER NOT NULL DEFAULT 0, avg_tokens DOUBLE PRECISION NOT NULL DEFAULT 0, "
    "avg_latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0, "
    "failure_kinds TEXT[] NOT NULL DEFAULT '{}', keywords TEXT[] NOT NULL DEFAULT '{}', "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_TOKEN_RE = re.compile(r"[a-z0-9_]+")

#: Router score weights; the three components sum to 1.0.
TFIDF_WEIGHT = 0.55
WIN_RATE_WEIGHT = 0.30
COMPLEMENT_WEIGHT = 0.15

#: Neutral win rate for an adapter with no paired decisions yet.
NEUTRAL_WIN_RATE = 0.5

#: Failure-kind Jaccard at or above this is "known incompatibility".
INCOMPATIBLE_JACCARD = 0.5

#: Keep the per-profile failure-kind vocabulary bounded.
MAX_FAILURE_KINDS = 32

_SCORE_DECIMALS = 6


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def behavior_routing_enabled() -> bool:
    """Feature flag: local complementary adapter routing (default off)."""

    return _env_flag("UC_ADAPTIVE_ADAPTER_ROUTING")


def _text_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a list/tuple/PostgreSQL-array text value into a clean string tuple."""

    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            text = text[1:-1]
        raw: list[Any] = text.split(",") if text else []
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = list(value)
    else:
        raw = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip().strip('"').strip("'")
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _running_average(previous: float, sample: float, count: int) -> float:
    if count <= 0:
        return float(previous)
    return ((count - 1) * float(previous) + float(sample)) / count


def tokenize(text: Any) -> tuple[str, ...]:
    """Lowercased ``[a-z0-9_]+`` tokens, dropping one-character noise."""

    if text is None:
        return ()
    return tuple(
        token for token in _TOKEN_RE.findall(str(text).lower()) if len(token) > 1
    )


@dataclass(frozen=True)
class AdapterBehaviorProfile:
    """Observed behavior of one adapter (wins/losses, cost, failure kinds)."""

    adapter: str
    wins: int = 0
    losses: int = 0
    failures: int = 0
    avg_tokens: float = 0.0
    avg_latency_ms: float = 0.0
    failure_kinds: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter", str(self.adapter or "").strip())
        object.__setattr__(self, "wins", max(0, _as_int(self.wins)))
        object.__setattr__(self, "losses", max(0, _as_int(self.losses)))
        object.__setattr__(self, "failures", max(0, _as_int(self.failures)))
        object.__setattr__(self, "avg_tokens", max(0.0, _as_float(self.avg_tokens)))
        object.__setattr__(
            self, "avg_latency_ms", max(0.0, _as_float(self.avg_latency_ms))
        )
        object.__setattr__(self, "failure_kinds", _text_tuple(self.failure_kinds))
        object.__setattr__(self, "keywords", _text_tuple(self.keywords))
        object.__setattr__(self, "metadata", _metadata_dict(self.metadata))

    @property
    def decisions(self) -> int:
        """Number of paired win/loss observations."""

        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        """``wins / (wins + losses)``; 0.5 when there is no history."""

        decisions = self.decisions
        if decisions <= 0:
            return NEUTRAL_WIN_RATE
        return self.wins / decisions

    def with_outcome(
        self,
        adapter: str,
        *,
        won: bool,
        tokens: float = 0,
        latency_ms: float = 0,
        failure_kind: str = "",
    ) -> AdapterBehaviorProfile:
        """Return a new frozen profile with one outcome folded in.

        A new profile starts from neutral 0.5 placement: every outcome is one
        sample for the running token/latency averages.  A failure kind is
        recorded (and counted) only when the outcome was not a win.
        """

        target = str(adapter or self.adapter).strip() or self.adapter
        wins = self.wins + (1 if won else 0)
        losses = self.losses + (0 if won else 1)
        decisions = wins + losses
        token_sample = max(0.0, _as_float(tokens))
        latency_sample = max(0.0, _as_float(latency_ms))
        avg_tokens = _running_average(self.avg_tokens, token_sample, decisions)
        avg_latency = _running_average(
            self.avg_latency_ms, latency_sample, decisions
        )
        failures = self.failures
        failure_kinds = self.failure_kinds
        kind = str(failure_kind or "").strip()
        if not won and kind:
            failures += 1
            if kind not in failure_kinds:
                failure_kinds = tuple((*failure_kinds, kind)[-MAX_FAILURE_KINDS:])
        return replace(
            self,
            adapter=target,
            wins=wins,
            losses=losses,
            failures=failures,
            avg_tokens=avg_tokens,
            avg_latency_ms=avg_latency,
            failure_kinds=failure_kinds,
            metadata=dict(self.metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "wins": int(self.wins),
            "losses": int(self.losses),
            "failures": int(self.failures),
            "avg_tokens": float(self.avg_tokens),
            "avg_latency_ms": float(self.avg_latency_ms),
            "failure_kinds": list(self.failure_kinds),
            "keywords": list(self.keywords),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> AdapterBehaviorProfile:
        if not isinstance(row, Mapping):
            raise ValueError("adapter profile row must be a mapping")
        adapter = str(row.get("adapter") or "").strip()
        if not adapter:
            raise ValueError("adapter profile row requires a non-empty adapter")
        return cls(
            adapter=adapter,
            wins=_as_int(row.get("wins")),
            losses=_as_int(row.get("losses")),
            failures=_as_int(row.get("failures")),
            avg_tokens=_as_float(row.get("avg_tokens")),
            avg_latency_ms=_as_float(row.get("avg_latency_ms")),
            failure_kinds=_text_tuple(row.get("failure_kinds")),
            keywords=_text_tuple(row.get("keywords")),
            metadata=_metadata_dict(row.get("metadata")),
        )


@dataclass(frozen=True)
class AdapterChoice:
    """One routing decision plus the runner-up adapters."""

    adapter: str
    score: float
    reason: str
    alternatives: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "score": float(self.score),
            "reason": self.reason,
            "alternatives": list(self.alternatives),
        }


class BehaviorProfileStore:
    """In-memory adapter behavior profiles (the durable mirror is PostgreSQL)."""

    def __init__(
        self,
        profiles: Iterable[AdapterBehaviorProfile] | Mapping[str, Any] = (),
    ) -> None:
        self._profiles: dict[str, AdapterBehaviorProfile] = {}
        source = profiles.values() if isinstance(profiles, Mapping) else profiles
        for profile in source:
            self.upsert(profile)

    def __len__(self) -> int:
        return len(self._profiles)

    def __iter__(self) -> Iterator[AdapterBehaviorProfile]:
        return iter(self.all())

    def __contains__(self, adapter: object) -> bool:
        return str(adapter or "").strip() in self._profiles

    def upsert(
        self,
        profile: AdapterBehaviorProfile | Mapping[str, Any],
    ) -> AdapterBehaviorProfile:
        if isinstance(profile, Mapping):
            profile = AdapterBehaviorProfile.from_dict(profile)
        if not isinstance(profile, AdapterBehaviorProfile):
            raise TypeError("profile must be an AdapterBehaviorProfile or mapping")
        if not profile.adapter:
            raise ValueError("adapter profile requires a non-empty adapter")
        self._profiles[profile.adapter] = profile
        return profile

    def get(self, adapter: str) -> AdapterBehaviorProfile | None:
        return self._profiles.get(str(adapter or "").strip())

    def all(self) -> tuple[AdapterBehaviorProfile, ...]:
        """Every profile, ordered by adapter name for deterministic routing."""

        return tuple(self._profiles[name] for name in sorted(self._profiles))

    def record_outcome(
        self,
        adapter: str,
        *,
        won: bool,
        tokens: float = 0,
        latency_ms: float = 0,
        failure_kind: str = "",
    ) -> AdapterBehaviorProfile:
        key = str(adapter or "").strip()
        if not key:
            raise ValueError("adapter is required")
        base = self._profiles.get(key) or AdapterBehaviorProfile(adapter=key)
        updated = base.with_outcome(
            key,
            won=won,
            tokens=tokens,
            latency_ms=latency_ms,
            failure_kind=failure_kind,
        )
        self._profiles[key] = updated
        return updated

    def load_rows(
        self, rows: Iterable[Mapping[str, Any]]
    ) -> tuple[AdapterBehaviorProfile, ...]:
        """Load mapping rows (DB rows / JSON) into the store."""

        if isinstance(rows, Mapping):
            rows = (rows,)
        loaded: list[AdapterBehaviorProfile] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if not str(row.get("adapter") or "").strip():
                continue
            loaded.append(self.upsert(AdapterBehaviorProfile.from_dict(row)))
        return tuple(loaded)

    def to_rows(self) -> list[dict[str, Any]]:
        """JSON/DB-safe rows for the PostgreSQL mirror."""

        return [
            {
                "adapter": profile.adapter,
                "wins": int(profile.wins),
                "losses": int(profile.losses),
                "failures": int(profile.failures),
                "avg_tokens": float(profile.avg_tokens),
                "avg_latency_ms": float(profile.avg_latency_ms),
                "failure_kinds": list(profile.failure_kinds),
                "keywords": list(profile.keywords),
            }
            for profile in self.all()
        ]

    @staticmethod
    def postgres_ddl() -> str:
        return postgres_ddl()


def postgres_ddl() -> str:
    """DDL for the durable adapter behavior profile table."""

    return POSTGRES_DDL


def _profile_terms(profile: AdapterBehaviorProfile) -> tuple[str, ...]:
    terms: list[str] = list(tokenize(profile.adapter))
    for keyword in profile.keywords:
        terms.extend(tokenize(keyword))
    for kind in profile.failure_kinds:
        terms.extend(tokenize(kind))
    return tuple(terms)


class LocalTfidfAdapterIndex:
    """Pure-Python TF-IDF cosine index over adapter profiles."""

    def __init__(
        self,
        profiles: Iterable[AdapterBehaviorProfile] | Mapping[str, Any] = (),
    ) -> None:
        self._profiles: dict[str, AdapterBehaviorProfile] = {}
        self._vectors: dict[str, dict[str, float]] = {}
        self._norms: dict[str, float] = {}
        self._idf: dict[str, float] = {}
        self._unknown_idf = 1.0
        self.refresh(profiles)

    def __len__(self) -> int:
        return len(self._profiles)

    @property
    def adapters(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def refresh(
        self,
        profiles: Iterable[AdapterBehaviorProfile] | Mapping[str, Any],
    ) -> None:
        source = profiles.values() if isinstance(profiles, Mapping) else profiles
        profiles_by_name: dict[str, AdapterBehaviorProfile] = {}
        documents: dict[str, tuple[str, ...]] = {}
        for profile in source:
            if not isinstance(profile, AdapterBehaviorProfile):
                continue
            if not profile.adapter:
                continue
            profiles_by_name[profile.adapter] = profile
            documents[profile.adapter] = _profile_terms(profile)
        self._profiles = profiles_by_name
        document_count = len(documents)
        self._unknown_idf = math.log(document_count + 1.0) + 1.0
        document_frequency: Counter = Counter()
        for terms in documents.values():
            for term in set(terms):
                document_frequency[term] += 1
        self._idf = {
            term: math.log((1.0 + document_count) / (1.0 + count)) + 1.0
            for term, count in document_frequency.items()
        }
        vectors: dict[str, dict[str, float]] = {}
        norms: dict[str, float] = {}
        for name, terms in documents.items():
            counts = Counter(terms)
            total = float(sum(counts.values()))
            vector: dict[str, float] = {}
            if total > 0:
                for term, count in counts.items():
                    vector[term] = (count / total) * self._idf.get(term, 0.0)
            vectors[name] = vector
            norms[name] = math.sqrt(
                sum(weight * weight for weight in vector.values())
            )
        self._vectors = vectors
        self._norms = norms

    def similarity(self, task_text: str, adapter: str) -> float:
        """TF-IDF cosine in ``[0, 1]``; 0.0 for an unknown adapter."""

        name = str(adapter or "").strip()
        document = self._vectors.get(name)
        if not document:
            return 0.0
        counts = Counter(tokenize(task_text))
        total = float(sum(counts.values()))
        if total <= 0:
            return 0.0
        dot = 0.0
        query_norm_sq = 0.0
        for term, count in counts.items():
            weight = (count / total) * self._idf.get(term, self._unknown_idf)
            query_norm_sq += weight * weight
            dot += weight * document.get(term, 0.0)
        document_norm = self._norms.get(name, 0.0)
        if query_norm_sq <= 0.0 or document_norm <= 0.0:
            return 0.0
        cosine = dot / (math.sqrt(query_norm_sq) * document_norm)
        return max(0.0, min(1.0, cosine))

    def rank(self, task_text: str) -> list[tuple[str, float]]:
        """Adapters by descending similarity, ties broken by adapter name."""

        scored = [
            (name, self.similarity(task_text, name)) for name in self._profiles
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored


def keyword_similarity(
    failure_kinds: Sequence[str] | str, failure_text: str
) -> float:
    """Jaccard overlap between an adapter's failure kinds and a failure text."""

    if isinstance(failure_kinds, str):
        kinds_text = failure_kinds
    else:
        kinds_text = " ".join(str(kind) for kind in failure_kinds)
    left = set(tokenize(kinds_text))
    right = set(tokenize(failure_text))
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def is_complementary(
    profile: AdapterBehaviorProfile, failure_text: str
) -> bool:
    """True when the profile shows no known incompatibility with the failure."""

    return (
        keyword_similarity(profile.failure_kinds, failure_text)
        < INCOMPATIBLE_JACCARD
    )


def _dedupe_names(values: Iterable[str] | str | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = str(value or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return tuple(names)


class ComplementaryAdapterRouter:
    """Pick a complementary adapter for a retry, using only local signals.

    The failed adapter is excluded whenever another candidate exists.  The
    winner maximizes ``0.55 * normalized tfidf + 0.30 * win_rate + 0.15 *
    complement`` with deterministic ties broken by adapter name.
    """

    def __init__(
        self,
        store: BehaviorProfileStore,
        index: LocalTfidfAdapterIndex | None = None,
    ) -> None:
        self._store = store
        self._index = index

    @property
    def store(self) -> BehaviorProfileStore:
        return self._store

    def choose(
        self,
        task_description: str,
        *,
        failed_adapter: str,
        candidates: Sequence[str] = (),
        failure_kind: str = "",
        failure_text: str = "",
    ) -> AdapterChoice | None:
        failed = str(failed_adapter or "").strip()
        requested = _dedupe_names(candidates)
        if requested:
            names = requested
        else:
            names = tuple(profile.adapter for profile in self._store.all())
        failed_excluded = False
        if failed and failed in names and len(names) > 1:
            names = tuple(name for name in names if name != failed)
            failed_excluded = True
        if not names:
            return None

        profiles = {
            name: self._store.get(name) or AdapterBehaviorProfile(adapter=name)
            for name in names
        }
        text = str(failure_text or "").strip()
        kind = str(failure_kind or "").strip()

        def _failure_similarity(failure_kinds: Sequence[str] | str) -> float:
            if kind and text:
                combined = f"{kind} {text}" if kind.lower() not in text.lower() else text
                return max(
                    keyword_similarity(failure_kinds, text),
                    keyword_similarity(failure_kinds, kind),
                    keyword_similarity(failure_kinds, combined),
                )
            return keyword_similarity(failure_kinds, text or kind)

        complementary = tuple(
            name
            for name in names
            if _failure_similarity(profiles[name].failure_kinds) < INCOMPATIBLE_JACCARD
        )
        filtered = bool(complementary) and len(complementary) < len(names)
        pool = complementary if complementary else names

        index = self._index
        if index is None:
            index = LocalTfidfAdapterIndex(profiles)
        task = str(task_description or "")
        raw = {
            name: max(0.0, float(index.similarity(task, name))) for name in pool
        }
        peak = max(raw.values()) if raw else 0.0
        normalized = {
            name: (value / peak if peak > 0.0 else 0.0)
            for name, value in raw.items()
        }

        scores: dict[str, float] = {}
        win_rates: dict[str, float] = {}
        complements: dict[str, float] = {}
        for name in pool:
            profile = profiles[name]
            win_rate = profile.win_rate
            complement = 1.0 - _failure_similarity(profile.failure_kinds)
            score = (
                TFIDF_WEIGHT * normalized[name]
                + WIN_RATE_WEIGHT * win_rate
                + COMPLEMENT_WEIGHT * complement
            )
            scores[name] = round(max(0.0, min(1.0, score)), _SCORE_DECIMALS)
            win_rates[name] = win_rate
            complements[name] = complement

        ranked = sorted(pool, key=lambda name: (-scores[name], name))
        winner = ranked[0]
        parts = [
            f"tfidf={normalized[winner]:.2f}",
            f"win_rate={win_rates[winner]:.2f}",
            f"complement={complements[winner]:.2f}",
        ]
        if failed_excluded:
            parts.append("failed_excluded")
        if filtered:
            parts.append("incompatible_filtered")
        if failed and winner == failed:
            parts.append("failed_fallback")
        return AdapterChoice(
            adapter=winner,
            score=scores[winner],
            reason="; ".join(parts),
            alternatives=tuple(ranked[1:]),
        )
