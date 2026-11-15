"""Versioned knowledge pages: a derived, revision-bound projection of claims.

A page is **not** an authority. It is a projection assembled from authoritative
records (evidence claims, plus the bounded survey/deepen observations) and it
keeps the ids of the records it was derived from. Pages are versioned in
``RuntimeState`` under :data:`NAMESPACE`; the authoritative claims themselves
stay in the ``knowledge_claims`` namespace owned by
:mod:`ultimate_coders.knowledge.evidence`.

Lifecycle::

    active --supersede(by_page_id)--> superseded
    active --retract--> retracted        (superseded may also be retracted)

Only ``active`` pages are current. ``visible`` additionally requires the reader
scope to be allowed by the page scope and, when both sides pin a revision, the
page's :class:`~ultimate_coders.knowledge.model.RevisionBinding` to apply to the
reader's revision. Retracted and superseded pages are never visible.

Pages are *derived*, so :meth:`KnowledgePages.rebuild` is idempotent: the same
scope, title and sources produce the same page id and the same content, and a
rebuild that changes nothing does not bump the stored version. Locked pages
refuse automatic mutation; ``unlock`` is the explicit human action that
re-enables it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..runtime_state import RecordConflictError, RuntimeState
from .deepen import summarize_deepen
from .model import (
    KnowledgeScope,
    RevisionBinding,
    Visibility,
    normalize_claim_status,
    normalize_visibility,
    safe_text,
    short_digest,
    utc_now_iso,
    visibility_rank,
)
from .survey import summarize_survey

__all__ = [
    "CAS_RETRIES",
    "LIFECYCLES",
    "LIFECYCLE_ACTIVE",
    "LIFECYCLE_RETRACTED",
    "LIFECYCLE_SUPERSEDED",
    "MAX_BODY_CHARS",
    "MAX_CLAIM_REFS",
    "MAX_EVIDENCE_IDS",
    "MAX_ID_CHARS",
    "MAX_LIST_LIMIT",
    "MAX_REASON_CHARS",
    "MAX_TITLE_CHARS",
    "NAMESPACE",
    "PAGE_ID_PREFIX",
    "KnowledgePages",
    "PageClaimRef",
    "VersionedPage",
    "page_id_for",
]

NAMESPACE = "knowledge_pages"
PAGE_ID_PREFIX = "page_"

LIFECYCLE_ACTIVE = "active"
LIFECYCLE_SUPERSEDED = "superseded"
LIFECYCLE_RETRACTED = "retracted"
LIFECYCLES = frozenset({LIFECYCLE_ACTIVE, LIFECYCLE_SUPERSEDED, LIFECYCLE_RETRACTED})
_TERMINAL_LIFECYCLES = frozenset({LIFECYCLE_SUPERSEDED, LIFECYCLE_RETRACTED})
_TERMINAL_CLAIM_STATUSES = frozenset({"superseded", "retracted"})

MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 20_000
MAX_CLAIM_REFS = 200
MAX_EVIDENCE_IDS = 100
MAX_REASON_CHARS = 1_000
MAX_ID_CHARS = 120
MAX_LIST_LIMIT = 500
MAX_REVISION_VALUE = 1_000_000_000
CAS_RETRIES = 3
_QUERY_BATCH = 500


# ---------------------------------------------------------------------------
# Bounded coercion helpers (never import another module's private helpers)
# ---------------------------------------------------------------------------


def _text(value: Any, *, limit: int = MAX_ID_CHARS) -> str:
    """Bounded, stripped text for a stored string field."""

    return safe_text(value, limit=limit).strip()


def _identifier(value: Any) -> str:
    """Bounded record id; empty when the value carries no usable text."""

    return _text(value, limit=MAX_ID_CHARS)


def _int(value: Any) -> int:
    """Clamp a stored revision/counter into ``[0, MAX_REVISION_VALUE]``."""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_REVISION_VALUE, number))


def _count(value: Any, *, limit: int) -> int:
    """Clamp a caller-supplied list/count bound into ``[0, limit]``."""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(limit, number))


def _visibility(value: Any) -> Visibility:
    """Tolerantly normalize a visibility; unknown values degrade to ATTEMPT."""

    try:
        return normalize_visibility(value)
    except ValueError:
        return Visibility.ATTEMPT


def _claim_status_text(value: Any) -> str:
    """Tolerantly normalize a claim status to its string value (or ``""``)."""

    try:
        return normalize_claim_status(value).value
    except ValueError:
        return ""


def _lifecycle(value: Any) -> str:
    """Tolerantly normalize a lifecycle; unknown non-empty values fail closed."""

    text = _text(value, limit=40).lower()
    if not text:
        return LIFECYCLE_ACTIVE
    if text in LIFECYCLES:
        return text
    # A record written by another schema is hidden rather than surfaced.
    return LIFECYCLE_RETRACTED


def _scope_of(value: Any) -> KnowledgeScope | None:
    """Coerce a scope (instance, mapping or attribute style); ``None`` if unknown."""

    if isinstance(value, KnowledgeScope):
        return value
    if isinstance(value, Mapping):
        if not any(
            field in value for field in ("tenant_id", "repo_id", "branch", "graph_id")
        ):
            return None
        return KnowledgeScope.from_dict(value)
    if hasattr(value, "tenant_id"):
        return KnowledgeScope(
            tenant_id=_text(getattr(value, "tenant_id", "") or "default", limit=80),
            repo_id=_text(getattr(value, "repo_id", ""), limit=120),
            branch=_text(getattr(value, "branch", ""), limit=120),
            graph_id=_text(getattr(value, "graph_id", ""), limit=120),
        )
    return None


def _binding_of(value: Any) -> RevisionBinding:
    """Coerce a revision binding (instance, mapping or attribute style)."""

    if isinstance(value, RevisionBinding):
        return value
    if isinstance(value, Mapping):
        return RevisionBinding.from_dict(value)
    if value is None:
        return RevisionBinding()
    fields = (
        "repo_id",
        "branch",
        "revision",
        "base_revision",
        "integration_ref",
        "integration_sha",
    )
    if any(hasattr(value, field) for field in fields):
        return RevisionBinding(
            repo_id=_text(getattr(value, "repo_id", ""), limit=120),
            branch=_text(getattr(value, "branch", ""), limit=120),
            revision=_text(getattr(value, "revision", ""), limit=120),
            base_revision=_text(getattr(value, "base_revision", ""), limit=120),
            integration_ref=_text(getattr(value, "integration_ref", ""), limit=120),
            integration_sha=_text(getattr(value, "integration_sha", ""), limit=120),
        )
    return RevisionBinding()


def _deepen_head(value: Any) -> str:
    """Best-effort head sha of a deepening (instance, mapping or attribute style)."""

    if value is None:
        return ""
    if isinstance(value, Mapping):
        return _identifier(value.get("head"))
    return _identifier(getattr(value, "head", ""))


def _claim_field(item: Any, name: str, default: Any = None) -> Any:
    """Read one field from a claim record in mapping or attribute style."""

    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _claim_items(claims: Any) -> list[Any]:
    """Materialize a claim iterable, tolerating a single record and garbage."""

    if claims is None:
        return []
    if isinstance(claims, Mapping):
        return [claims] if claims.get("claim_id") else []
    if isinstance(claims, (str, bytes)):
        return []
    try:
        items = list(claims)
    except TypeError:
        return []
    return [item for item in items if item is not None]


def _evidence_ids(values: Any) -> tuple[str, ...]:
    """Bounded, duplicate-free evidence ids from an iterable or single id."""

    if values is None:
        return ()
    if isinstance(values, str):
        candidates: Iterable[Any] = (values,)
    elif isinstance(values, Mapping):
        return ()
    else:
        try:
            candidates = list(values)
        except TypeError:
            return ()
    collected: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        evidence_id = _identifier(candidate)
        if not evidence_id or evidence_id in seen:
            continue
        seen.add(evidence_id)
        collected.append(evidence_id)
    return tuple(sorted(collected)[:MAX_EVIDENCE_IDS])


def _claim_parts(
    claim: Any,
    page_scope: KnowledgeScope,
) -> tuple[PageClaimRef, tuple[str, ...]] | None:
    """Derive one claim reference plus its evidence ids, or drop the claim.

    Claims are filtered by scope (a claim whose scope cannot be read in the
    page scope is not cited) and by status (superseded/retracted claims are
    never cited). Malformed claims are skipped rather than raising.
    """

    claim_id = _identifier(_claim_field(claim, "claim_id"))
    if not claim_id:
        return None
    status = _claim_status_text(_claim_field(claim, "status"))
    if status in _TERMINAL_CLAIM_STATUSES:
        return None
    claim_scope_value = _claim_field(claim, "scope", None)
    if claim_scope_value is not None:
        claim_scope = _scope_of(claim_scope_value)
        if claim_scope is not None and not claim_scope.allows_read(page_scope):
            return None
    ref = PageClaimRef(
        claim_id=claim_id,
        visibility=_visibility(_claim_field(claim, "visibility")),
        status=status,
    )
    return ref, _evidence_ids(_claim_field(claim, "evidence_ids"))


def _refs_from_claims(
    claims: Iterable[Any],
    page_scope: KnowledgeScope,
) -> tuple[tuple[PageClaimRef, ...], tuple[str, ...]]:
    """Collect bounded claim refs and their evidence ids in deterministic order."""

    by_id: dict[str, PageClaimRef] = {}
    evidence_by_claim: dict[str, tuple[str, ...]] = {}
    for item in claims:
        parts = _claim_parts(item, page_scope)
        if parts is None:
            continue
        ref, evidence = parts
        if ref.claim_id in by_id:
            continue
        by_id[ref.claim_id] = ref
        evidence_by_claim[ref.claim_id] = evidence
    retained = [by_id[claim_id] for claim_id in sorted(by_id)][:MAX_CLAIM_REFS]
    evidence: list[str] = []
    seen: set[str] = set()
    for ref in retained:
        for evidence_id in evidence_by_claim.get(ref.claim_id, ()):
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            evidence.append(evidence_id)
    return tuple(retained), tuple(sorted(evidence)[:MAX_EVIDENCE_IDS])


def _refs_from_values(
    values: Iterable[Any],
    page_scope: KnowledgeScope,
) -> tuple[tuple[PageClaimRef, ...], tuple[str, ...]]:
    """Normalize explicit claim refs (PageClaimRef, claim records or mappings)."""

    items = [item for item in values if item is not None]
    plain = [item for item in items if isinstance(item, PageClaimRef)]
    records = [item for item in items if not isinstance(item, PageClaimRef)]
    refs, evidence = _refs_from_claims(records, page_scope)
    by_id: dict[str, PageClaimRef] = {ref.claim_id: ref for ref in refs}
    for item in plain:
        if item.status in _TERMINAL_CLAIM_STATUSES:
            continue
        by_id.setdefault(item.claim_id, item)
    retained = [by_id[claim_id] for claim_id in sorted(by_id)][:MAX_CLAIM_REFS]
    return tuple(retained), evidence


def _page_id_value(value: Any) -> str:
    """Bounded page id with the deterministic prefix preserved when present."""

    return _identifier(value)


def _content_equal(left: VersionedPage, right: VersionedPage) -> bool:
    """Whether two pages carry the same content (ignoring version/timestamps)."""

    return (
        left.page_id == right.page_id
        and left.scope == right.scope
        and left.title == right.title
        and left.body == right.body
        and left.binding == right.binding
        and left.claim_refs == right.claim_refs
        and left.evidence_ids == right.evidence_ids
        and left.lifecycle == right.lifecycle
        and left.superseded_by == right.superseded_by
        and left.locked == right.locked
        and left.human_locked == right.human_locked
        and left.lifecycle_reason == right.lifecycle_reason
    )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageClaimRef:
    """Reference from a page to one authoritative claim record it derives from."""

    claim_id: str
    visibility: Visibility = Visibility.ATTEMPT
    status: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", _identifier(self.claim_id))
        object.__setattr__(self, "visibility", _visibility(self.visibility))
        object.__setattr__(self, "status", _claim_status_text(self.status))

    def to_dict(self) -> dict:
        """JSON-safe projection of the reference."""

        return {
            "claim_id": self.claim_id,
            "visibility": self.visibility.value,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PageClaimRef:
        """Rebuild a claim reference from stored data, tolerating missing fields."""

        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            claim_id=_identifier(data.get("claim_id")),
            visibility=_visibility(data.get("visibility")),
            status=_claim_status_text(data.get("status")),
        )

    def to_summary(self) -> dict:
        """Log-safe projection: ids and status only (never claim statement)."""

        return {
            "claim_id": self.claim_id,
            "visibility": self.visibility.value,
            "status": self.status,
        }


@dataclass(frozen=True)
class VersionedPage:
    """One revision-bound knowledge page derived from authoritative records.

    ``body`` is page content, never source text; ``to_summary()`` deliberately
    excludes it so pages can be logged or emitted in events. ``claim_refs``
    and ``evidence_ids`` are the only facts a page is allowed to assert, and
    ``lifecycle_reason`` records why a lifecycle transition happened.
    """

    page_id: str
    scope: KnowledgeScope = KnowledgeScope()
    title: str = ""
    body: str = ""
    binding: RevisionBinding = RevisionBinding()
    claim_refs: tuple[PageClaimRef, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    lifecycle: str = LIFECYCLE_ACTIVE
    revision: int = 0
    created_at: str = ""
    updated_at: str = ""
    superseded_by: str = ""
    locked: bool = False
    human_locked: bool = False
    lifecycle_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "page_id", _page_id_value(self.page_id))
        scope = _scope_of(self.scope)
        object.__setattr__(self, "scope", scope if scope is not None else KnowledgeScope())
        object.__setattr__(self, "title", _text(self.title, limit=MAX_TITLE_CHARS))
        object.__setattr__(self, "body", safe_text(self.body, limit=MAX_BODY_CHARS).strip())
        object.__setattr__(self, "binding", _binding_of(self.binding))
        refs: list[PageClaimRef] = []
        seen: set[str] = set()
        for item in tuple(self.claim_refs or ()):
            ref: PageClaimRef | None = None
            if isinstance(item, PageClaimRef):
                ref = item
            elif isinstance(item, Mapping):
                ref = PageClaimRef.from_dict(item)
            elif _identifier(_claim_field(item, "claim_id")):
                parts = _claim_parts(item, self.scope)
                ref = parts[0] if parts is not None else None
            if ref is None or not ref.claim_id or ref.claim_id in seen:
                continue
            seen.add(ref.claim_id)
            refs.append(ref)
            if len(refs) >= MAX_CLAIM_REFS:
                break
        refs.sort(key=lambda ref: ref.claim_id)
        object.__setattr__(self, "claim_refs", tuple(refs))
        object.__setattr__(self, "evidence_ids", _evidence_ids(self.evidence_ids))
        object.__setattr__(self, "lifecycle", _lifecycle(self.lifecycle))
        object.__setattr__(self, "revision", _int(self.revision))
        object.__setattr__(self, "created_at", _text(self.created_at, limit=40))
        object.__setattr__(self, "updated_at", _text(self.updated_at, limit=40))
        object.__setattr__(self, "superseded_by", _page_id_value(self.superseded_by))
        human_locked = bool(self.human_locked)
        object.__setattr__(self, "human_locked", human_locked)
        object.__setattr__(self, "locked", bool(self.locked) or human_locked)
        object.__setattr__(
            self, "lifecycle_reason", _text(self.lifecycle_reason, limit=MAX_REASON_CHARS)
        )

    @property
    def is_current(self) -> bool:
        """True while the page is active (superseded/retracted pages are not)."""

        return self.lifecycle == LIFECYCLE_ACTIVE

    @property
    def is_terminal(self) -> bool:
        """True once the page is superseded or retracted."""

        return self.lifecycle in _TERMINAL_LIFECYCLES

    @property
    def effective_visibility(self) -> Visibility:
        """Furthest-travelling visibility among the claims this page cites."""

        best = Visibility.ATTEMPT
        best_rank = visibility_rank(best)
        for ref in self.claim_refs:
            rank = visibility_rank(ref.visibility)
            if rank > best_rank:
                best = ref.visibility
                best_rank = rank
        return best

    def claim_ids(self) -> tuple[str, ...]:
        """Ids of the claims this page cites, in deterministic order."""

        return tuple(ref.claim_id for ref in self.claim_refs)

    def applies_to(self, revision: str) -> bool:
        """Whether this page's revision binding is valid for ``revision``."""

        return self.binding.applies_to(revision)

    def to_dict(self) -> dict:
        """JSON-safe projection of the page (body included)."""

        return {
            "page_id": self.page_id,
            "scope": self.scope.to_dict(),
            "title": self.title,
            "body": self.body,
            "binding": self.binding.to_dict(),
            "claim_refs": [ref.to_dict() for ref in self.claim_refs],
            "evidence_ids": list(self.evidence_ids),
            "lifecycle": self.lifecycle,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "superseded_by": self.superseded_by,
            "locked": self.locked,
            "human_locked": self.human_locked,
            "lifecycle_reason": self.lifecycle_reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> VersionedPage:
        """Rebuild a page from stored data, tolerating malformed parts."""

        data = payload if isinstance(payload, Mapping) else {}
        refs = data.get("claim_refs") or ()
        return cls(
            page_id=_page_id_value(data.get("page_id")),
            scope=_scope_of(data.get("scope")),
            title=_text(data.get("title"), limit=MAX_TITLE_CHARS),
            body=safe_text(data.get("body"), limit=MAX_BODY_CHARS).strip(),
            binding=_binding_of(data.get("binding")),
            claim_refs=tuple(item for item in refs if isinstance(item, (PageClaimRef, Mapping))),
            evidence_ids=_evidence_ids(data.get("evidence_ids")),
            lifecycle=_lifecycle(data.get("lifecycle", data.get("state"))),
            revision=_int(data.get("revision")),
            created_at=_text(data.get("created_at"), limit=40),
            updated_at=_text(data.get("updated_at"), limit=40),
            superseded_by=_page_id_value(data.get("superseded_by")),
            locked=bool(data.get("locked")),
            human_locked=bool(data.get("human_locked")),
            lifecycle_reason=_text(data.get("lifecycle_reason"), limit=MAX_REASON_CHARS),
        )

    def to_summary(self) -> dict:
        """Log-safe projection: ids, lifecycle and counts only, never the body."""

        return {
            "page_id": self.page_id,
            "scope": self.scope.key(),
            "title": self.title,
            "lifecycle": self.lifecycle,
            "revision": self.revision,
            "claims": len(self.claim_refs),
            "evidence": len(self.evidence_ids),
            "visibility": self.effective_visibility.value,
            "locked": self.locked,
            "superseded_by": self.superseded_by,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# Page id and body derivation
# ---------------------------------------------------------------------------


def page_id_for(scope: Any, title: str = "") -> str:
    """Deterministic page id derived from the scope key and the title.

    The same ``(scope, title)`` pair always yields the same id, so a rebuild
    without an explicit ``page_id`` re-materializes the same page instead of
    creating duplicates. Unknown scopes yield ``""`` (no id is invented).
    """

    page_scope = _scope_of(scope)
    if page_scope is None:
        return ""
    return PAGE_ID_PREFIX + short_digest(
        [page_scope.key(), _text(title, limit=MAX_TITLE_CHARS)], 24
    )


def _default_title(scope: KnowledgeScope) -> str:
    """Factual fallback title from the scope identity."""

    fallback = scope.branch or scope.repo_id or scope.tenant_id or "knowledge"
    return _text(fallback, limit=MAX_TITLE_CHARS)


def _auto_body(
    refs: tuple[PageClaimRef, ...],
    evidence_ids: tuple[str, ...],
    binding: RevisionBinding,
    survey: Any,
    deepen: Any,
) -> str:
    """Deterministic body for auto-derived pages: bounded facts and counts only."""

    lines = [f"claims: {len(refs)}", f"evidence: {len(evidence_ids)}"]
    revision = _identifier(binding.revision)
    lines.append(f"revision: {revision[:12] if revision else 'unpinned'}")
    if survey is not None:
        summary = summarize_survey(survey)
        lines.append(
            f"survey: {summary.get('modules', 0)} modules, "
            f"{summary.get('symbols', 0)} symbols"
        )
    if deepen is not None:
        history = summarize_deepen(deepen)
        lines.append(
            f"history: {history.get('commits', 0)} commits, "
            f"{history.get('paths', 0)} paths, {history.get('lines_changed', 0)} lines changed"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _store_data(page: VersionedPage) -> dict:
    """Stored projection: page fields plus the indexed lifecycle/scope keys."""

    data = page.to_dict()
    data["state"] = page.lifecycle
    data["scope_key"] = page.scope.key()
    return data


def _page_from_data(data: Any) -> VersionedPage | None:
    """Parse one stored row, returning ``None`` instead of raising on garbage."""

    if not isinstance(data, Mapping):
        return None
    if not data:
        return None
    try:
        page = VersionedPage.from_dict(data)
    except (TypeError, ValueError, AttributeError):
        return None
    return page if page.page_id else None


class KnowledgePages:
    """Versioned page store backed by the authoritative :class:`RuntimeState`.

    The store owns no facts of its own: every page cites claim/evidence ids and
    every mutation is a CAS update on a ``RuntimeState`` record with an audit
    row. Read paths skip malformed rows instead of raising; write paths raise
    ``ValueError``/``KeyError`` for caller errors.
    """

    def __init__(self, state: RuntimeState) -> None:
        """Bind the store to an existing runtime state database."""

        self._state = state

    # -- read path ---------------------------------------------------------

    def get(self, page_id: str) -> VersionedPage | None:
        """Read one page by id; ``None`` when it is missing or malformed."""

        key = _page_id_value(page_id)
        if not key:
            return None
        versioned = self._state.versioned_get(NAMESPACE, key)
        if versioned is None:
            return None
        return _page_from_data(versioned.data)

    def _read_versioned(self, page_id: str) -> tuple[VersionedPage, int] | None:
        """Read one page plus its stored record version."""

        key = _page_id_value(page_id)
        if not key:
            return None
        versioned = self._state.versioned_get(NAMESPACE, key)
        if versioned is None:
            return None
        page = _page_from_data(versioned.data)
        if page is None:
            return None
        return page, versioned.version

    def _iter_pages(self, limit: int) -> Iterable[VersionedPage]:
        """Iterate stored pages deterministically, bounded by ``limit``."""

        collected = 0
        cursor = ""
        while collected < limit:
            rows = self._state.query(NAMESPACE, limit=_QUERY_BATCH, after=cursor)
            if not rows:
                break
            for row in rows:
                page = _page_from_data(row)
                if page is None:
                    continue
                yield page
                collected += 1
                if collected >= limit:
                    break
            cursor = str(rows[-1].get("key", cursor))
            if len(rows) < _QUERY_BATCH:
                break

    def list(
        self,
        *,
        scope: Any = None,
        lifecycle: str | None = None,
        limit: int = 50,
    ) -> tuple[VersionedPage, ...]:
        """List pages, optionally filtered by readable scope and lifecycle.

        ``scope`` is the reader scope: a page is listed when the page scope
        allows that reader. ``lifecycle`` is matched exactly; an unknown value
        yields an empty tuple rather than raising. Results are ordered by page
        id and bounded by ``limit`` (1..500).
        """

        bound = _count(limit, limit=MAX_LIST_LIMIT) or 1
        reader = _scope_of(scope) if scope is not None else None
        if scope is not None and reader is None:
            return ()
        wanted: str | None = None
        if lifecycle is not None:
            wanted = _lifecycle(lifecycle)
            if wanted != _text(lifecycle, limit=40).lower():
                return ()
        collected: list[VersionedPage] = []
        for page in self._iter_pages(bound):
            if reader is not None and not page.scope.allows_read(reader):
                continue
            if wanted is not None and page.lifecycle != wanted:
                continue
            collected.append(page)
        collected.sort(key=lambda page: page.page_id)
        return tuple(collected)

    def visible(
        self,
        *,
        scope: Any,
        reader_revision: str = "",
        min_visibility: Any = Visibility.ATTEMPT,
    ) -> tuple[VersionedPage, ...]:
        """Return the active pages a reader scope may see at a revision.

        A page is visible when it is active, the reader scope is allowed by the
        page scope, its effective visibility reaches ``min_visibility``, and -
        when both the page binding and ``reader_revision`` pin a revision - the
        binding applies to that revision. Retracted and superseded pages are
        never returned. Sorted by visibility (furthest first) then page id.
        """

        reader = _scope_of(scope)
        if reader is None:
            return ()
        required_rank = visibility_rank(_visibility(min_visibility))
        revision = _text(reader_revision, limit=120)
        pages: list[VersionedPage] = []
        for page in self._iter_pages(MAX_LIST_LIMIT):
            if not page.is_current:
                continue
            if not page.scope.allows_read(reader):
                continue
            if visibility_rank(page.effective_visibility) < required_rank:
                continue
            if revision and page.binding.revision and not page.binding.applies_to(revision):
                continue
            pages.append(page)
        pages.sort(key=lambda page: (-visibility_rank(page.effective_visibility), page.page_id))
        return tuple(pages)

    # -- write path --------------------------------------------------------

    def _create_page(self, page: VersionedPage) -> VersionedPage:
        """Create one page record; a concurrent creation returns the existing page."""

        now = utc_now_iso()
        candidate = replace(page, revision=1, created_at=now, updated_at=now)
        box: dict[str, VersionedPage] = {}

        def change(current: dict) -> dict:
            existing = _page_from_data(current)
            if existing is not None:
                box["page"] = existing
                return _store_data(existing)
            box["page"] = candidate
            return _store_data(candidate)

        self._state.mutate(
            NAMESPACE,
            candidate.page_id,
            change,
            audit={"action": "create", "page_id": candidate.page_id},
        )
        return box.get("page", candidate)

    def _mutate_page(
        self,
        page_id: str,
        build: Callable[[VersionedPage], VersionedPage],
        *,
        action: str,
        reason: str = "",
        terminal_policy: str = "return",
        allow_locked: bool = False,
        allow_terminal: bool = False,
    ) -> VersionedPage:
        """CAS-apply one page mutation with idempotent no-op detection."""

        key = _page_id_value(page_id)
        if not key:
            raise KeyError("knowledge page id is required")
        bounded_reason = _text(reason, limit=MAX_REASON_CHARS)
        for _attempt in range(CAS_RETRIES + 1):
            versioned = self._state.versioned_get(NAMESPACE, key)
            current = _page_from_data(versioned.data) if versioned is not None else None
            if current is None:
                raise KeyError(f"unknown knowledge page: {key}")
            candidate = build(current)
            if not isinstance(candidate, VersionedPage):
                raise TypeError("page mutation must return a VersionedPage")
            if _content_equal(current, candidate):
                return current
            if current.is_terminal:
                if terminal_policy == "raise":
                    raise ValueError(f"page {key} is {current.lifecycle}; it cannot be updated")
                if not allow_terminal:
                    return current
            if (current.locked or current.human_locked) and not allow_locked:
                raise ValueError("page is locked; unlock before changing it")
            updated = replace(
                candidate,
                page_id=key,
                revision=current.revision + 1,
                created_at=current.created_at,
                updated_at=utc_now_iso(),
            )
            try:
                self._state.mutate(
                    NAMESPACE,
                    key,
                    lambda _data: _store_data(updated),
                    expected_version=versioned.version,
                    audit={"action": action, "page_id": key, "reason": bounded_reason},
                )
            except RecordConflictError:
                continue
            return updated
        raise RecordConflictError(f"knowledge page {key} changed concurrently; retry")

    def add(
        self,
        *,
        scope: Any,
        title: str = "",
        body: str = "",
        binding: Any = None,
        claim_refs: Iterable[Any] = (),
        evidence_ids: Iterable[Any] = (),
        page_id: str = "",
    ) -> VersionedPage:
        """Author one page explicitly; idempotent for identical content.

        Use this for human-authored or maintenance pages. A page that already
        exists with different content raises ``ValueError`` - use
        :meth:`update` instead. Claim records may be passed in ``claim_refs``;
        their ids and evidence ids are copied, never invented.
        """

        page_scope = _scope_of(scope)
        if page_scope is None:
            raise ValueError("knowledge pages require a KnowledgeScope")
        supplied_title = _text(title, limit=MAX_TITLE_CHARS)
        key = _page_id_value(page_id) or page_id_for(page_scope, supplied_title)
        if not key:
            raise ValueError("knowledge page id could not be derived")
        refs, derived_evidence = _refs_from_values(tuple(claim_refs or ()), page_scope)
        explicit_evidence = _evidence_ids(evidence_ids)
        merged_evidence = _evidence_ids(tuple(derived_evidence) + tuple(explicit_evidence))
        if not refs and not merged_evidence:
            raise ValueError("knowledge page must reference at least one claim or evidence id")
        candidate = VersionedPage(
            page_id=key,
            scope=page_scope,
            title=supplied_title or _default_title(page_scope),
            body=body,
            binding=_binding_of(binding),
            claim_refs=refs,
            evidence_ids=merged_evidence,
            lifecycle=LIFECYCLE_ACTIVE,
        )
        existing = self.get(key)
        if existing is not None:
            if _content_equal(existing, candidate):
                return existing
            raise ValueError("knowledge page already exists; use update()")
        return self._create_page(candidate)

    def update(
        self,
        page_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        binding: Any = None,
        claim_refs: Iterable[Any] | None = None,
        evidence_ids: Iterable[Any] | None = None,
        reason: str = "",
    ) -> VersionedPage:
        """Update an active page's authored content or bindings.

        ``None`` keeps an existing value. Passing ``claim_refs`` replaces the
        references (terminal claims are dropped); passing ``evidence_ids``
        replaces the evidence ids. A locked or terminal page is refused.
        """

        def build(current: VersionedPage) -> VersionedPage:
            new_title = _text(title, limit=MAX_TITLE_CHARS) if title is not None else current.title
            new_body = (
                safe_text(body, limit=MAX_BODY_CHARS).strip()
                if body is not None
                else current.body
            )
            new_binding = _binding_of(binding) if binding is not None else current.binding
            if claim_refs is None:
                new_refs = current.claim_refs
                new_evidence = current.evidence_ids
            else:
                new_refs, derived = _refs_from_values(tuple(claim_refs), current.scope)
                if evidence_ids is None:
                    new_evidence = _evidence_ids(tuple(derived) + tuple(current.evidence_ids))
                else:
                    new_evidence = _evidence_ids(evidence_ids)
            if evidence_ids is not None and claim_refs is None:
                new_evidence = _evidence_ids(evidence_ids)
            if not new_refs and not new_evidence:
                raise ValueError("knowledge page must reference at least one claim or evidence id")
            return replace(
                current,
                title=new_title or current.title,
                body=new_body,
                binding=new_binding,
                claim_refs=new_refs,
                evidence_ids=new_evidence,
            )

        return self._mutate_page(
            page_id,
            build,
            action="update",
            reason=reason,
            terminal_policy="raise",
        )

    def rebuild(
        self,
        *,
        scope: Any,
        survey: Any = None,
        deepen: Any = None,
        claims: Iterable[Any] = (),
        title: str = "",
        body: str = "",
        page_id: str = "",
        binding: Any = None,
    ) -> VersionedPage:
        """Re-derive a page from authoritative records (idempotent projection).

        The page id is deterministic from ``(scope, title)`` unless ``page_id``
        is given. Claim refs and evidence ids are derived from ``claims``; a
        rebuild supplies no claims for one of two reasons:

        * the page already exists - its refs are preserved;
        * it does not - a derived page cannot be created without claim
          references, so ``ValueError`` is raised (author such a page with
          :meth:`add`).

        ``binding`` overrides the derived revision binding, letting a caller
        that knows where the projection was anchored (e.g. the merge arbiter
        after an integration receipt) pin the landed revision/integration ref
        instead of relying on a deepening head. When omitted the binding is
        derived as before (deepening head, else the existing page's revision,
        preserving landed integration).

        When claims are supplied but none survive filtering (terminal, malformed
        or unreadable scope) an existing page is retracted instead of being
        left stale, and creating a page raises ``ValueError``. Missing
        ``body``/``title`` are filled from bounded facts (counts only), never
        from source text. A rebuild that changes nothing returns the existing
        page without bumping its version.
        """

        page_scope = _scope_of(scope)
        if page_scope is None:
            raise ValueError("knowledge pages require a KnowledgeScope")
        supplied_title = _text(title, limit=MAX_TITLE_CHARS)
        key = _page_id_value(page_id) or page_id_for(page_scope, supplied_title)
        if not key:
            raise ValueError("knowledge page id could not be derived")
        supplied_body = safe_text(body, limit=MAX_BODY_CHARS).strip()
        claim_items = _claim_items(claims)
        refs: tuple[PageClaimRef, ...] = ()
        evidence: tuple[str, ...] = ()
        claims_supplied = bool(claim_items)
        if claims_supplied:
            refs, evidence = _refs_from_claims(claim_items, page_scope)
            if not refs:
                existing = self.get(key)
                if existing is None:
                    raise ValueError("rebuild requires at least one usable claim reference")
                if existing.is_terminal:
                    return existing
                return self.retract(key, reason="rebuild: no usable claim references")
        if not claims_supplied and self._read_versioned(key) is None:
            raise ValueError("rebuild requires claim references to derive a page")

        def build(current: VersionedPage) -> VersionedPage:
            new_refs = refs if claims_supplied else current.claim_refs
            new_evidence = evidence if claims_supplied else current.evidence_ids
            new_title = supplied_title or current.title or _default_title(page_scope)
            new_binding = (
                _binding_of(binding)
                if binding is not None
                else _rebuild_binding(page_scope, deepen, current)
            )
            new_body = (
                supplied_body
                or current.body
                or _auto_body(new_refs, new_evidence, new_binding, survey, deepen)
            )
            return replace(
                current,
                scope=page_scope,
                title=new_title,
                body=new_body,
                binding=new_binding,
                claim_refs=new_refs,
                evidence_ids=new_evidence,
            )

        existing = self._read_versioned(key)
        if existing is None:
            candidate = build(
                VersionedPage(
                    page_id=key,
                    scope=page_scope,
                    title=supplied_title or _default_title(page_scope),
                )
            )
            return self._create_page(candidate)
        return self._mutate_page(key, build, action="rebuild", terminal_policy="return")

    def supersede(self, page_id: str, *, by_page_id: str, reason: str = "") -> VersionedPage:
        """Mark a page superseded by an existing active successor page.

        The predecessor and successor must share a tenant, the successor must be
        active and must not be the page itself. Already superseded/retracted
        pages are returned unchanged (idempotent). Locked pages refuse the
        transition until :meth:`unlock` is called explicitly.
        """

        key = _page_id_value(page_id)
        successor_id = _page_id_value(by_page_id)
        if not key or not successor_id:
            raise ValueError("both page_id and by_page_id are required")
        if key == successor_id:
            raise ValueError("a page cannot supersede itself")
        successor = self.get(successor_id)
        if successor is None:
            raise ValueError(f"unknown successor page: {successor_id}")
        if not successor.is_current:
            raise ValueError("successor page is not active")
        bounded_reason = _text(reason, limit=MAX_REASON_CHARS)

        def build(current: VersionedPage) -> VersionedPage:
            if current.is_terminal:
                return current
            if successor.scope.tenant_id != current.scope.tenant_id:
                raise ValueError("successor page is in a different tenant")
            return replace(
                current,
                lifecycle=LIFECYCLE_SUPERSEDED,
                superseded_by=successor_id,
                lifecycle_reason=bounded_reason,
            )

        return self._mutate_page(
            key,
            build,
            action="supersede",
            reason=bounded_reason,
            terminal_policy="return",
        )

    def retract(self, page_id: str, *, reason: str = "") -> VersionedPage:
        """Retract a page so it is never visible again (idempotent).

        A superseded page may still be retracted; an already retracted page is
        returned unchanged. A locked page must be unlocked first, which is the
        explicit human action that authorizes the transition.
        """

        bounded_reason = _text(reason, limit=MAX_REASON_CHARS)

        def build(current: VersionedPage) -> VersionedPage:
            if current.lifecycle == LIFECYCLE_RETRACTED:
                return current
            return replace(
                current,
                lifecycle=LIFECYCLE_RETRACTED,
                lifecycle_reason=bounded_reason,
            )

        return self._mutate_page(
            page_id,
            build,
            action="retract",
            reason=bounded_reason,
            terminal_policy="return",
            allow_terminal=True,
        )

    def lock(self, page_id: str, *, reason: str = "") -> VersionedPage:
        """Lock a page against automatic mutation; idempotent when already locked."""

        bounded_reason = _text(reason, limit=MAX_REASON_CHARS)

        def build(current: VersionedPage) -> VersionedPage:
            if current.locked and current.human_locked:
                return current
            return replace(current, locked=True, human_locked=True, lifecycle_reason=bounded_reason)

        return self._mutate_page(
            page_id,
            build,
            action="lock",
            reason=bounded_reason,
            terminal_policy="return",
            allow_locked=True,
        )

    def unlock(self, page_id: str, *, reason: str = "") -> VersionedPage:
        """Clear a page lock (the explicit human action); idempotent when unlocked."""

        bounded_reason = _text(reason, limit=MAX_REASON_CHARS)

        def build(current: VersionedPage) -> VersionedPage:
            if not current.locked and not current.human_locked:
                return current
            return replace(
                current, locked=False, human_locked=False, lifecycle_reason=bounded_reason
            )

        return self._mutate_page(
            page_id,
            build,
            action="unlock",
            reason=bounded_reason,
            terminal_policy="return",
            allow_locked=True,
        )


def _rebuild_binding(
    page_scope: KnowledgeScope,
    deepen: Any,
    existing: VersionedPage,
) -> RevisionBinding:
    """Derive the revision binding for a rebuild, preserving landed integration."""

    head = _deepen_head(deepen)
    revision = head or existing.binding.revision
    return RevisionBinding(
        repo_id=page_scope.repo_id,
        branch=page_scope.branch,
        revision=revision,
        base_revision=existing.binding.base_revision,
        integration_ref=existing.binding.integration_ref,
        integration_sha=existing.binding.integration_sha,
    )
