"""Focused tests for versioned knowledge pages (P4 pages projection)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from ultimate_coders.knowledge.deepen import GitDeepen
from ultimate_coders.knowledge.model import KnowledgeScope, RevisionBinding, Visibility
from ultimate_coders.knowledge.pages import (
    NAMESPACE,
    KnowledgePages,
    PageClaimRef,
    VersionedPage,
    page_id_for,
)
from ultimate_coders.knowledge.survey import ModuleSurvey
from ultimate_coders.runtime_state import RuntimeState

SCOPE = KnowledgeScope(tenant_id="default", repo_id="repo", branch="main")
OTHER_SCOPE = KnowledgeScope(tenant_id="default", repo_id="other", branch="main")


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Isolate the runtime state database for every test in this module."""

    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("UC_KNOWLEDGE_STAGE", raising=False)


@pytest.fixture()
def state(tmp_path):
    """Runtime state backed by a per-test SQLite database."""

    return RuntimeState(tmp_path / "pages-state.sqlite3", database_url="")


@pytest.fixture()
def pages(state):
    """Page store bound to the isolated runtime state."""

    return KnowledgePages(state)


def _claim(
    claim_id,
    *,
    scope=SCOPE,
    visibility="graph",
    status="validated",
    evidence=("ev-1",),
):
    """Build one duck-typed claim record (mapping style) for a page rebuild."""

    return {
        "claim_id": claim_id,
        "statement": "claim statement that must never reach a page body",
        "scope": scope,
        "visibility": visibility,
        "status": status,
        "evidence_ids": list(evidence),
    }


def test_page_id_is_deterministic_from_scope_and_title() -> None:
    page_id = page_id_for(SCOPE, "API")
    assert page_id == page_id_for(SCOPE, "API")
    assert page_id == page_id_for(SCOPE.to_dict(), "API")
    assert page_id.startswith("page_")
    assert page_id != page_id_for(SCOPE, "Other")
    assert page_id != page_id_for(
        KnowledgeScope(tenant_id="other", repo_id="repo", branch="main"), "API"
    )
    assert page_id_for(object(), "API") == ""


def test_rebuild_derives_counts_and_is_idempotent(pages) -> None:
    page = pages.rebuild(
        scope=SCOPE,
        claims=[_claim("claim_b"), _claim("claim_a")],
        title="API",
    )
    assert page.page_id == page_id_for(SCOPE, "API")
    assert page.lifecycle == "active"
    assert page.revision == 1
    assert page.title == "API"
    assert page.claim_ids() == ("claim_a", "claim_b")
    assert page.evidence_ids == ("ev-1",)
    assert page.binding.repo_id == "repo"
    assert page.binding.branch == "main"
    assert page.binding.revision == ""
    assert page.body.splitlines() == ["claims: 2", "evidence: 1", "revision: unpinned"]

    again = pages.rebuild(
        scope=SCOPE,
        claims=[_claim("claim_a"), _claim("claim_b")],
        title="API",
    )
    assert again.page_id == page.page_id
    assert again.revision == 1
    assert again == pages.get(page.page_id)


def test_rebuild_ids_are_stable_across_stores(tmp_path) -> None:
    first = KnowledgePages(RuntimeState(tmp_path / "first.sqlite3", database_url=""))
    second = KnowledgePages(RuntimeState(tmp_path / "second.sqlite3", database_url=""))
    left = first.rebuild(scope=SCOPE, claims=[_claim("claim_a")])
    right = second.rebuild(scope=SCOPE, claims=[_claim("claim_a")])
    assert left.page_id == right.page_id
    assert left.title == right.title == "main"


def test_rebuild_binds_deepen_revision_and_hides_stale(pages) -> None:
    deepen = GitDeepen(head="deadbeefcafe")
    page = pages.rebuild(scope=SCOPE, deepen=deepen, claims=[_claim("claim_a")], title="API")
    assert page.binding.revision == "deadbeefcafe"
    assert "revision: deadbeefcafe" in page.body
    assert "history: 0 commits, 0 paths, 0 lines changed" in page.body


def test_rebuild_accepts_an_explicit_binding_override(pages) -> None:
    """A caller that saw the landing (arbiter) pins the landed revision."""

    binding = RevisionBinding(
        repo_id="repo",
        branch="main",
        revision="feedface1234",
        integration_ref="origin/main",
        integration_sha="feedface1234",
    )
    page = pages.rebuild(
        scope=SCOPE,
        deepen=GitDeepen(head="deadbeefcafe"),
        claims=[_claim("claim_a")],
        title="API",
        binding=binding,
    )
    # The explicit binding wins over the deepening head...
    assert page.binding.revision == "feedface1234"
    assert page.binding.repo_id == "repo"
    assert page.binding.branch == "main"
    assert "revision: feedface1234" in page.body
    # ...and the landed integration is preserved (the page is not stale).
    assert page.binding.integration_ref == "origin/main"
    assert page.binding.integration_sha == "feedface1234"
    assert page.binding.has_integration is True
    # Readers pinned to the landed revision (or a short form of it) see the
    # page; a different revision does not.
    assert pages.visible(scope=SCOPE, reader_revision="feedface1234") == (page,)
    assert pages.visible(scope=SCOPE, reader_revision="feedface") == (page,)
    assert pages.visible(scope=SCOPE, reader_revision="0badcafe0bad") == ()


def test_rebuild_accepts_a_mapping_binding_override(pages) -> None:
    page = pages.rebuild(
        scope=SCOPE,
        claims=[_claim("claim_a")],
        title="API",
        binding={"branch": "main", "revision": "abc1234", "integration_ref": "origin/main"},
    )
    assert page.binding.revision == "abc1234"
    assert page.binding.integration_ref == "origin/main"
    assert pages.visible(scope=SCOPE) == (page,)
    assert pages.visible(scope=SCOPE, reader_revision="abc1234") == (page,)
    assert pages.visible(scope=SCOPE, reader_revision="deadbeefcafe") == ()


def test_rebuild_includes_survey_counts(pages) -> None:
    survey = (ModuleSurvey(path="a.py", module="a"),)
    page = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API", survey=survey)
    assert "survey: 1 modules, 0 symbols" in page.body


def test_rebuild_filters_terminal_and_unreadable_claims(pages) -> None:
    claims = [
        _claim("claim_ok"),
        _claim("claim_old", status="superseded"),
        _claim("claim_gone", status="retracted"),
        _claim("claim_foreign", scope=OTHER_SCOPE),
        {"statement": "malformed"},
    ]
    page = pages.rebuild(scope=SCOPE, claims=claims, title="Filtered")
    assert page.claim_ids() == ("claim_ok",)
    assert page.evidence_ids == ("ev-1",)


def test_rebuild_accepts_attribute_style_claims(pages) -> None:
    claim = SimpleNamespace(
        claim_id="claim_ns",
        scope=SCOPE,
        visibility="repository",
        status="promoted",
        evidence_ids=("ev-ns",),
    )
    page = pages.rebuild(scope=SCOPE, claims=[claim], title="Namespace")
    assert page.claim_ids() == ("claim_ns",)
    assert page.claim_refs[0].visibility == Visibility.REPOSITORY
    assert page.claim_refs[0].status == "promoted"
    assert page.evidence_ids == ("ev-ns",)


def test_rebuild_without_usable_claims_retracts_or_refuses(pages) -> None:
    page = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API")
    retracted = pages.rebuild(
        scope=SCOPE,
        claims=[_claim("claim_a", status="retracted")],
        title="API",
    )
    assert retracted.page_id == page.page_id
    assert retracted.lifecycle == "retracted"
    assert retracted.lifecycle_reason == "rebuild: no usable claim references"
    assert pages.visible(scope=SCOPE) == ()
    with pytest.raises(ValueError, match="at least one usable claim reference"):
        pages.rebuild(
            scope=SCOPE,
            claims=[_claim("claim_x", status="superseded")],
            title="Fresh",
        )


def test_rebuild_without_claims_requires_an_existing_page(pages) -> None:
    with pytest.raises(ValueError, match="requires claim references"):
        pages.rebuild(scope=SCOPE, title="Orphan")
    page = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="Keep")
    assert pages.rebuild(scope=SCOPE, title="Keep") == page


def test_rebuild_returns_terminal_pages_unchanged(pages) -> None:
    page = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API")
    retracted = pages.retract(page.page_id, reason="obsolete")
    again = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API")
    assert again == retracted
    assert again.lifecycle == "retracted"


def test_rebuild_content_change_bumps_revision_and_keeps_created_at(pages) -> None:
    page = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API")
    updated = pages.rebuild(
        scope=SCOPE,
        claims=[_claim("claim_a"), _claim("claim_b")],
        title="API",
    )
    assert updated.revision == page.revision + 1
    assert updated.claim_ids() == ("claim_a", "claim_b")
    assert updated.created_at == page.created_at
    assert updated.updated_at >= page.updated_at


def test_rebuild_preserves_landed_binding(pages) -> None:
    page = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API")
    pages.update(
        page.page_id,
        binding=RevisionBinding(revision="deadbeefcafe", base_revision="base123"),
    )
    again = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API")
    assert again.binding.revision == "deadbeefcafe"
    assert again.binding.base_revision == "base123"
    assert again.binding.repo_id == "repo"
    assert again.binding.branch == "main"


def test_add_requires_a_reference_and_is_idempotent(pages) -> None:
    with pytest.raises(ValueError, match="must reference"):
        pages.add(scope=SCOPE, title="Manual")
    with pytest.raises(ValueError, match="KnowledgeScope"):
        pages.add(scope=None, title="Manual", evidence_ids=["ev-1"])
    first = pages.add(scope=SCOPE, title="Manual", body="body", evidence_ids=["ev-9"])
    assert first.revision == 1
    assert first.claim_ids() == ()
    assert first.evidence_ids == ("ev-9",)
    assert pages.add(scope=SCOPE, title="Manual", body="body", evidence_ids=["ev-9"]) == first
    with pytest.raises(ValueError, match="already exists"):
        pages.add(scope=SCOPE, title="Manual", body="other", evidence_ids=["ev-9"])
    from_claim = pages.add(scope=SCOPE, title="FromClaim", claim_refs=[_claim("claim_add")])
    assert from_claim.claim_ids() == ("claim_add",)
    assert from_claim.evidence_ids == ("ev-1",)
    custom = pages.add(scope=SCOPE, page_id="page_custom", evidence_ids="ev-custom")
    assert custom.page_id == "page_custom"
    assert custom.evidence_ids == ("ev-custom",)


def test_update_replaces_content_and_merges_evidence(pages) -> None:
    page = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API")
    renamed = pages.update(page.page_id, title="API v2", body="hand written", reason="docs")
    assert renamed.title == "API v2"
    assert renamed.body == "hand written"
    assert renamed.revision == page.revision + 1
    assert renamed.lifecycle_reason == ""
    replaced = pages.update(page.page_id, claim_refs=[_claim("claim_b", evidence=("ev-2",))])
    assert replaced.claim_ids() == ("claim_b",)
    assert replaced.evidence_ids == ("ev-1", "ev-2")
    narrowed = pages.update(page.page_id, evidence_ids=["ev-3"])
    assert narrowed.claim_ids() == ("claim_b",)
    assert narrowed.evidence_ids == ("ev-3",)
    with pytest.raises(KeyError):
        pages.update("page_missing", body="x")


def test_update_refuses_to_strip_every_reference(pages) -> None:
    page = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a", evidence=())], title="API")
    assert page.evidence_ids == ()
    with pytest.raises(ValueError, match="must reference"):
        pages.update(
            page.page_id,
            claim_refs=[_claim("claim_b", status="superseded", evidence=())],
        )


def test_supersede_marks_predecessor(pages) -> None:
    old = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="Old")
    new = pages.rebuild(scope=SCOPE, claims=[_claim("claim_b")], title="New")
    superseded = pages.supersede(old.page_id, by_page_id=new.page_id, reason="replaced")
    assert superseded.lifecycle == "superseded"
    assert superseded.superseded_by == new.page_id
    assert superseded.lifecycle_reason == "replaced"
    assert superseded.is_terminal and not superseded.is_current
    assert superseded.revision == old.revision + 1
    assert [page.page_id for page in pages.visible(scope=SCOPE)] == [new.page_id]
    assert pages.supersede(old.page_id, by_page_id=new.page_id, reason="replaced") == superseded
    with pytest.raises(ValueError, match="cannot supersede itself"):
        pages.supersede(new.page_id, by_page_id=new.page_id)
    with pytest.raises(ValueError, match="unknown successor"):
        pages.supersede(new.page_id, by_page_id="page_missing")
    with pytest.raises(ValueError, match="not active"):
        pages.supersede(new.page_id, by_page_id=old.page_id)
    with pytest.raises(ValueError, match="both page_id and by_page_id"):
        pages.supersede(old.page_id, by_page_id="")
    closed = pages.retract(old.page_id, reason="closed")
    assert closed.lifecycle == "retracted"


def test_supersede_rejects_cross_tenant_successor(pages) -> None:
    other = KnowledgeScope(tenant_id="other", repo_id="repo", branch="main")
    old = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="Old")
    foreign = pages.rebuild(scope=other, claims=[_claim("claim_f", scope=other)], title="Foreign")
    with pytest.raises(ValueError, match="different tenant"):
        pages.supersede(old.page_id, by_page_id=foreign.page_id)


def test_retract_is_terminal_and_idempotent(pages) -> None:
    page = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API")
    retracted = pages.retract(page.page_id, reason="obsolete")
    assert retracted.lifecycle == "retracted"
    assert retracted.lifecycle_reason == "obsolete"
    assert retracted.revision == page.revision + 1
    assert pages.visible(scope=SCOPE) == ()
    assert pages.get(page.page_id) == retracted
    assert pages.retract(page.page_id, reason="again") == retracted
    with pytest.raises(KeyError):
        pages.retract("page_missing")
    with pytest.raises(ValueError, match="cannot be updated"):
        pages.update(page.page_id, body="new body")


def test_lock_blocks_mutation_until_unlocked(pages) -> None:
    page = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API")
    successor = pages.rebuild(scope=SCOPE, claims=[_claim("claim_b")], title="Next")
    locked = pages.lock(page.page_id, reason="frozen")
    assert locked.locked and locked.human_locked
    assert locked.lifecycle_reason == "frozen"
    assert locked.revision == page.revision + 1
    assert pages.lock(page.page_id) == locked
    with pytest.raises(ValueError, match="locked"):
        pages.update(page.page_id, body="new body")
    with pytest.raises(ValueError, match="locked"):
        pages.retract(page.page_id)
    with pytest.raises(ValueError, match="locked"):
        pages.supersede(page.page_id, by_page_id=successor.page_id)
    with pytest.raises(ValueError, match="locked"):
        pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API", body="different")
    assert pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API") == locked
    unlocked = pages.unlock(page.page_id, reason="reopened")
    assert not unlocked.locked and not unlocked.human_locked
    assert unlocked.lifecycle_reason == "reopened"
    assert pages.update(page.page_id, body="new body").body == "new body"


def test_list_filters_scope_lifecycle_and_limit(pages) -> None:
    old = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="Old")
    new = pages.rebuild(scope=SCOPE, claims=[_claim("claim_b")], title="New")
    pages.rebuild(scope=OTHER_SCOPE, claims=[_claim("claim_o", scope=OTHER_SCOPE)], title="Other")
    pages.supersede(old.page_id, by_page_id=new.page_id)
    assert [page.page_id for page in pages.list(scope=SCOPE, lifecycle="active")] == [new.page_id]
    assert [page.page_id for page in pages.list(scope=SCOPE, lifecycle="superseded")] == [
        old.page_id
    ]
    assert pages.list(scope=SCOPE, lifecycle="bogus") == ()
    assert pages.list(scope=object()) == ()
    other = pages.list(scope=OTHER_SCOPE)
    assert len(other) == 1 and other[0].scope.repo_id == "other"
    every = pages.list()
    assert len(every) == 3
    assert [page.page_id for page in every] == sorted(page.page_id for page in every)
    limited = pages.list(limit=1)
    assert len(limited) == 1
    assert limited[0].page_id == every[0].page_id


def test_visible_respects_scope_visibility_and_terminal_state(pages) -> None:
    api = pages.rebuild(scope=SCOPE, claims=[_claim("claim_api", visibility="graph")], title="API")
    repo = pages.rebuild(
        scope=SCOPE,
        claims=[_claim("claim_repo", visibility="repository")],
        title="Repo",
    )
    manual = pages.add(scope=SCOPE, title="Manual", evidence_ids=["ev-manual"])
    assert repo.effective_visibility == Visibility.REPOSITORY
    assert manual.effective_visibility == Visibility.ATTEMPT
    assert [page.page_id for page in pages.visible(scope=SCOPE)] == [
        repo.page_id,
        api.page_id,
        manual.page_id,
    ]
    assert pages.visible(scope=SCOPE, min_visibility=Visibility.REPOSITORY) == (repo,)
    assert [page.page_id for page in pages.visible(scope=SCOPE, min_visibility="graph")] == [
        repo.page_id,
        api.page_id,
    ]
    assert pages.visible(scope=OTHER_SCOPE) == ()
    assert pages.visible(scope=None) == ()
    pages.retract(api.page_id, reason="gone")
    pages.supersede(repo.page_id, by_page_id=manual.page_id, reason="replaced")
    assert pages.visible(scope=SCOPE) == (pages.get(manual.page_id),)


def test_summaries_never_echo_the_page_body(pages) -> None:
    page = pages.add(
        scope=SCOPE,
        title="Manual",
        body="TOP SECRET PAGE BODY",
        evidence_ids=["ev-1"],
    )
    summary = page.to_summary()
    assert "body" not in summary
    assert "TOP SECRET" not in str(summary)
    assert summary["page_id"] == page.page_id
    assert summary["scope"] == SCOPE.key()
    assert summary["claims"] == 0
    assert summary["evidence"] == 1
    assert "TOP SECRET" not in str(pages.get(page.page_id).to_summary())
    assert "TOP SECRET" in page.to_dict()["body"]


def test_records_tolerate_malformed_rows(state, pages) -> None:
    state.mutate(NAMESPACE, "junk", lambda current: {"foo": "bar"})
    state.mutate(
        NAMESPACE,
        "page_weird",
        lambda current: {
            "page_id": "page_weird",
            "lifecycle": "bogus",
            "claim_refs": "junk",
            "evidence_ids": [None, "ev"],
            "revision": "x",
        },
    )
    assert pages.get("junk") is None
    weird = pages.get("page_weird")
    assert weird is not None
    assert weird.lifecycle == "retracted"
    assert weird.claim_refs == ()
    assert weird.evidence_ids == ("ev",)
    assert weird.revision == 0
    assert [page.page_id for page in pages.list()] == ["page_weird"]
    assert pages.list(lifecycle="retracted") == (weird,)
    assert pages.visible(scope=SCOPE) == ()


def test_page_records_round_trip_and_parse_tolerantly(pages) -> None:
    page = pages.rebuild(scope=SCOPE, claims=[_claim("claim_a")], title="API")
    assert VersionedPage.from_dict(page.to_dict()) == page
    tolerant = VersionedPage.from_dict(
        {
            "page_id": "page_x",
            "lifecycle": "mystery",
            "claim_refs": [{"claim_id": "c1", "visibility": "bogus", "status": "bogus"}, "junk"],
            "evidence_ids": [None, "ev"],
            "locked": True,
            "title": "t" * 400,
        }
    )
    assert tolerant.lifecycle == "retracted"
    assert [ref.claim_id for ref in tolerant.claim_refs] == ["c1"]
    assert tolerant.claim_refs[0].visibility == Visibility.ATTEMPT
    assert tolerant.claim_refs[0].status == ""
    assert tolerant.evidence_ids == ("ev",)
    assert tolerant.locked
    assert len(tolerant.title) == 200
    assert VersionedPage.from_dict({"page_id": "page_e"}).lifecycle == "active"
    ref = PageClaimRef.from_dict({"claim_id": "c2", "visibility": "graph", "status": "promoted"})
    assert ref.visibility == Visibility.GRAPH
    assert ref.status == "promoted"
    assert ref.to_dict() == {"claim_id": "c2", "visibility": "graph", "status": "promoted"}
