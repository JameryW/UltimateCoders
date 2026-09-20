"""Synthetic-corpus tests for scripts/check-spec-refs.py (#675 slice C).

`scripts/**` is in no CI workflow's `paths`, so this guard (and its exemption
tables) could rot without anything noticing.  These tests pin the mechanism that
stops the tables from becoming a silent blanket, and
`.github/workflows/ci-scripts.yml` runs them -- that workflow deliberately has NO
`paths` filter, which is the right shape for a corpus that is every `*.md` under
`.trellis/spec/**` and (since T41 / #691) `docs/**`.

Three properties of the guard shape this harness:

* its filename has a hyphen, so it cannot be imported by name -- load it with
  ``importlib.util.spec_from_file_location``;
* ``ROOT`` / ``SPEC_ROOTS`` are module-level constants read at call time, so a
  synthetic corpus is built by monkeypatching those two (plus clearing
  ``_ACK_CACHE``, which memoises the per-spec banner reads).  ``SPEC_ROOTS`` is a
  TUPLE since T41 (#691), so a synthetic corpus can exercise both namespaces;
* it has no import-time side effects (the scan runs under
  ``if __name__ == "__main__"``), so importing it is safe.

The last three tests run against the *real* repo.  They are the enforceable
half of the decision made in #675 slice C: the dangling *count* stays advisory
(a mention is not a reference), but the repo asserts its own state -- every
dangling mention is either fixed or carries a documented reason.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

GUARD_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "check-spec-refs.py"

ALPHA = "backend/alpha-spec.md"
ALPHA_PATH = ".trellis/spec/backend/alpha-spec.md"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_spec_refs_under_test", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_files(root: pathlib.Path, files: dict[str, str]) -> None:
    """Write bytes, not text: `Path.write_text(newline=...)` needs Python 3.10 and
    the CI matrix still includes 3.9, while the default would translate newlines
    and leave a `\\r` on every line the guard then reads as prose."""
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode("utf-8"))


@pytest.fixture
def guard(tmp_path):
    """The guard pointed at an empty synthetic repo, with both tables detached."""
    module = _load_guard()
    module.ROOT = tmp_path
    # BOTH roots (T41 / #691).  They live under tmp_path so the real repository is
    # never read, and they are separate NAMESPACES: `_short_of` strips exactly one
    # prefix, and the isolation tests below pin that a rule cannot cross over.
    module.SPEC_ROOTS = (tmp_path / ".trellis" / "spec", tmp_path / "docs")
    module._ACK_CACHE.clear()
    module.MENTION_EXEMPT = ()
    module.SUBJECT_REMOVED = {}
    for root in module.SPEC_ROOTS:
        root.mkdir(parents=True, exist_ok=True)
    return module


def _dangling_rows(module):
    return [r for r in module.collect()
            if r["kind"] == "mention" and r["verdict"] == module.DANGLING]


# --------------------------------------------------------------------------
# Invariant 3 -- a bare wildcard is file-wide in disguise
# --------------------------------------------------------------------------


def test_bare_wildcard_pattern_is_reported(guard):
    guard.MENTION_EXEMPT = ((ALPHA, "*", 1, "looks narrow but is not"),)
    _write_files(guard.ROOT, {ALPHA_PATH: "See `ghost.rs`.\n"})
    problems = guard.exemption_self_check(guard.collect())
    assert any("bare wildcard" in p for p in problems)
    # ...and it is NOT also reported as a dead rule (the elif, not two ifs).
    assert not any("matches nothing" in p for p in problems)


# --------------------------------------------------------------------------
# Invariant 4 -- a rule must not be broader than its own reason
# --------------------------------------------------------------------------


def test_pattern_wider_than_its_reason_is_reported(guard):
    """`ghost.*` is not a bare wildcard, so invariant 3 cannot see it -- yet it
    swallows a second mention the rule's reason does not cover.  Measured as
    ablation M3 in T26; this is the hole slice C was blocked on."""
    guard.MENTION_EXEMPT = ((ALPHA, "ghost.*", 1, "one illustrative path"),)
    _write_files(guard.ROOT, {ALPHA_PATH: "See `ghost.rs` and `ghost.py`.\n"})
    problems = guard.exemption_self_check(guard.collect())
    assert any("hit count drifted" in p for p in problems)
    assert not any("bare wildcard" in p for p in problems)


def test_declared_count_must_match_the_corpus(guard):
    guard.MENTION_EXEMPT = ((ALPHA, "ghost.rs", 9, "nine is a lie"),)
    _write_files(guard.ROOT, {ALPHA_PATH: "See `ghost.rs`.\n"})
    problems = guard.exemption_self_check(guard.collect())
    assert any("declares 9 mention(s) but matches 1" in p for p in problems)


# --------------------------------------------------------------------------
# Invariant 1 -- a rule that exempts nothing is a false claim
# --------------------------------------------------------------------------


def test_dead_rule_is_reported_without_a_second_count_complaint(guard):
    guard.MENTION_EXEMPT = ((ALPHA, "never-mentioned.rs", 1, "stale claim"),)
    _write_files(guard.ROOT, {ALPHA_PATH: "See `ghost.rs`.\n"})
    problems = guard.exemption_self_check(guard.collect())
    assert any("matches nothing" in p for p in problems)
    assert not any("hit count drifted" in p for p in problems)


# --------------------------------------------------------------------------
# Invariant 2 -- a file-level exemption must be declared by the spec itself
# --------------------------------------------------------------------------


def test_subject_removed_banner_controls_the_exemption(guard):
    guard.SUBJECT_REMOVED = {ALPHA: ("abc1234", "subject removed in abc1234")}

    # Without the banner the exemption is withheld, and the self-check says why.
    _write_files(guard.ROOT, {ALPHA_PATH: "See `ghost.rs`.\n"})
    rows = guard.collect()
    dangling = [r for r in rows
                if r["kind"] == "mention" and r["verdict"] == guard.DANGLING]
    assert len(dangling) == 1
    assert guard._mention_exemption(dangling[0]["spec"], dangling[0]["ref"]) is None
    assert any("banner missing" in p for p in guard.exemption_self_check(rows))

    # With it, the same mention is exempt and the tables are consistent again.
    guard._ACK_CACHE.clear()
    _write_files(guard.ROOT, {ALPHA_PATH: "See `ghost.rs`.\n\nRemoved in abc1234.\n"})
    assert guard.exemption_self_check(guard.collect()) == []
    assert guard._mention_exemption(ALPHA_PATH, "ghost.rs") is not None


def test_subject_removed_naming_a_missing_spec_is_reported(guard):
    guard.SUBJECT_REMOVED = {"backend/never-existed.md": ("abc1234", "gone")}
    problems = guard.exemption_self_check(guard.collect())
    assert any("does not exist" in p for p in problems)


# --------------------------------------------------------------------------
# The two mention verdicts are distinguishable ONLY by `verdict`
# --------------------------------------------------------------------------


def test_ambiguous_and_dangling_are_distinct_verdicts(guard):
    """Both have `target is None`, so filtering on the target conflates them.

    That was the T25 triage bug: `not row["target"]` yielded 68 rows while the
    guard reported 47, because the extra 21 were ambiguous (a basename that
    matches several files) rather than dangling.
    """
    _write_files(guard.ROOT, {
        ALPHA_PATH: "See `dup.rs` and `ghost.rs`.\n",
        "crates/one/dup.rs": "fn one() {}\n",
        "crates/two/dup.rs": "fn two() {}\n",
    })
    mentions = {r["ref"]: r for r in guard.collect() if r["kind"] == "mention"}
    assert mentions["dup.rs"]["verdict"] == guard.MENTION_AMBIGUOUS
    assert mentions["ghost.rs"]["verdict"] == guard.DANGLING
    assert mentions["dup.rs"]["target"] is None
    assert mentions["ghost.rs"]["target"] is None
    assert mentions["dup.rs"]["candidates"]
    assert mentions["ghost.rs"]["candidates"] == []
    # The distinction that matters for triage: only one of them is dangling.
    assert [r["ref"] for r in _dangling_rows(guard)] == ["ghost.rs"]


def test_an_untriaged_dangling_mention_is_not_a_problem(guard):
    """The guard never fails on a mention -- `unclassified` is a summary count."""
    _write_files(guard.ROOT, {ALPHA_PATH: "See `ghost.rs`.\n"})
    rows = guard.collect()
    dangling = _dangling_rows(guard)
    assert len(dangling) == 1
    assert guard._mention_exemption(dangling[0]["spec"], "ghost.rs") is None
    assert guard.exemption_self_check(rows) == []


# --------------------------------------------------------------------------
# A bold span is an anchor only when the span IS an identifier (T24; pinned T29)
# --------------------------------------------------------------------------


def test_bold_prose_is_not_a_symbol_anchor(guard):
    """`**...**` counts as a symbol anchor only when the span itself is a name.

    T24 (slice B) tightened this from "any bold span" to identifier-exact and
    measured the corpus effect (`STALE 27 -> 26`, `OK 113 -> 114`).  Until T29
    nothing pinned it: reverting the filter left all 11 tests green while the real
    corpus silently regained a false STALE (`89 ok / 1 stale` -> `88 ok / 2
    stale`).  This test is that pin.

    Mechanism being pinned: the guard picks the symbol whose definition sits
    *closest* to the referenced line, so one prose word that happens to be a
    defined name elsewhere in the target file is enough to drag the row out of its
    range -- and a pointer at a location must not go STALE off prose.  `GhostThing`
    is deliberately undefined in the target, so under the tightening the row has no
    resolvable symbol at all (OK); the loose form resolves the prose word `delete`
    at line 40, i.e. 38 lines away (STALE).
    """
    spec_line = ("See `crates/one/target.py:2` -- "
                 "**Handling `GhostThing` when delete runs**.")

    # The tightened semantics, asserted directly on the anchor set.
    symbols = guard._symbols_on(spec_line)
    assert "GhostThing" in symbols, "the tick span must still be an anchor"
    assert "delete" not in symbols, "prose inside a bold span is not an anchor"

    target = "\n".join(["# filler"] * 39 + ["def delete():", "    pass"])
    _write_files(guard.ROOT, {
        "crates/one/target.py": target,
        ".trellis/spec/backend/bold-spec.md": spec_line + "\n",
    })

    rows = [r for r in guard.collect() if r["kind"] == "ref"]
    assert len(rows) == 1
    row = rows[0]
    assert row["target"] == "crates/one/target.py"
    assert row["symbol"] is None, "the prose word must not become the row's symbol"
    assert row["verdict"] == "OK", "no resolvable symbol -> no offset -> OK"


# --------------------------------------------------------------------------
# Two roots (T41 / #691)
#
# The ticket's whole content is scope, so every test here is about the SECOND
# root: that it reaches the verdict (A/B/C), that it is what makes A/B/C
# visible rather than incidental (D, the necessary-condition proof), that the
# two namespaces cannot cover for one another (E), and that a rule written for
# the docs namespace is corpus-checked like any other (F).
# --------------------------------------------------------------------------


def test_both_roots_are_scanned(guard):
    """A spec under `docs/` must produce rows, and the rows must say which root."""
    _write_files(guard.ROOT, {
        "crates/one/target.py": "def f():\n    return 1\n",
        ".trellis/spec/backend/spec.md": "see `crates/one/target.py:1`\n",
        "docs/architecture/note.md": "see `crates/one/target.py:2`\n",
    })
    rows = [r for r in guard.collect() if r["kind"] == "ref"]
    assert {r["spec"] for r in rows} == {
        ".trellis/spec/backend/spec.md", "docs/architecture/note.md"}, rows
    assert {r["verdict"] for r in rows} == {"OK"}, rows


def test_docs_reference_with_a_wrong_directory_is_structural(guard):
    """Ablation A -- the shape that was actually broken in the real repo
    (`uc-types/src/agent.rs` where the file lives at `crates/uc-types/src/agent.rs`)."""
    _write_files(guard.ROOT, {
        "crates/one/target.py": "x = 1\n",
        "docs/architecture/note.md": "see `one/target.py:1`\n",
    })
    rows = [r for r in guard.collect() if r["kind"] == "ref"]
    assert [r["verdict"] for r in rows] == ["PATH_FORM"], rows


def test_docs_reference_past_the_end_is_structural(guard):
    """Ablation B."""
    _write_files(guard.ROOT, {
        "crates/one/target.py": "x = 1\n",
        "docs/architecture/note.md": "see `crates/one/target.py:99`\n",
    })
    rows = [r for r in guard.collect() if r["kind"] == "ref"]
    assert [r["verdict"] for r in rows] == ["OUT_OF_RANGE"], rows


def test_docs_reference_to_a_nonexistent_file_is_structural(guard):
    """Ablation C."""
    _write_files(guard.ROOT, {
        "docs/architecture/note.md": "see `crates/one/ghost.py:1`\n",
    })
    rows = [r for r in guard.collect() if r["kind"] == "ref"]
    assert [r["verdict"] for r in rows] == ["MISSING_FILE"], rows


def test_the_docs_root_is_what_makes_those_visible(guard):
    """Ablation D -- the necessary-condition proof.

    The same corpus, with the second root removed, reports NOTHING.  Without
    this, A/B/C could be passing on some incidental property of the fixture
    (e.g. the guard reading a directory it was not asked to read).
    """
    _write_files(guard.ROOT, {
        "crates/one/target.py": "x = 1\n",
        "docs/architecture/note.md": (
            "a `one/target.py:1`\n"
            "b `crates/one/target.py:99`\n"
            "c `crates/one/ghost.py:1`\n"
        ),
    })
    with_docs = [r["verdict"] for r in guard.collect() if r["kind"] == "ref"]
    assert with_docs == ["PATH_FORM", "OUT_OF_RANGE", "MISSING_FILE"], with_docs

    guard.SPEC_ROOTS = (guard.ROOT / ".trellis" / "spec",)
    without_docs = [r for r in guard.collect() if r["kind"] == "ref"]
    assert without_docs == [], without_docs


def test_a_short_name_cannot_serve_two_namespaces(guard):
    """Ablation E -- namespace isolation.

    A rule is keyed by the path RELATIVE TO ITS ROOT, which was unambiguous with
    one root.  With two, the same short can exist under each, and a rule written
    for one would silently fire in the other.  Asserted against the corpus.
    """
    _write_files(guard.ROOT, {
        "crates/one/target.py": "x = 1\n",
        ".trellis/spec/guides/twin.md": "see `crates/one/target.py`\n",
        "docs/guides/twin.md": "see `crates/one/target.py`\n",
    })
    problems = guard.exemption_self_check(guard.collect())
    assert any("not namespace-unique" in p for p in problems), problems
    assert any("guides/twin.md" in p for p in problems), problems


def test_a_docs_rule_is_corpus_checked_like_any_other(guard):
    """Ablation F -- the rule added for `docs/agents/domain.md` is not a blanket.

    It carries a declared hit count, so when its mentions stop matching the rule
    is reported dead/drifted rather than quietly widening.
    """
    guard.MENTION_EXEMPT = (("agents/domain.md", "CONTEXT.md", 2, "fixture rule"),)
    _write_files(guard.ROOT, {
        "docs/agents/domain.md": "read `CONTEXT.md`, `CONTEXT.md` and `CONTEXT.md`\n",
    })
    problems = guard.exemption_self_check(guard.collect())
    assert any("hit count drifted" in p for p in problems), problems
    assert any("declares 2" in p for p in problems), problems


def test_real_corpus_docs_root_is_live():
    """The second root is not decorative in the real repo.

    `docs/` contributes specs to the corpus, its one dangling name has a REASONED
    exemption (0 unclassified), and the exemption tables are still in sync.
    """
    guard = _load_guard()
    rows = guard.collect()
    docs_specs = sorted({r["spec"] for r in rows if r["spec"].startswith("docs/")})
    assert len(docs_specs) >= 6, docs_specs
    assert guard.exemption_self_check(rows) == []

    context = [r for r in rows
               if r["ref"] == "CONTEXT.md" and r["verdict"] == guard.DANGLING]
    assert len(context) == 2, context
    assert all(guard._mention_exemption(r["spec"], r["ref"]) for r in context), context


# --------------------------------------------------------------------------
# The real corpus
# --------------------------------------------------------------------------


def test_real_corpus_exemption_tables_are_in_sync():
    """Every documented rule still matches, and every banner is still declared."""
    guard = _load_guard()
    assert guard.exemption_self_check(guard.collect()) == []


def test_real_corpus_has_no_untriaged_dangling_mention():
    """The repo's own claim, deliberately not a property of the tool.

    A new dangling mention fails here on purpose: the fix is a one-line reasoned
    rule, and the failure mode being avoided is an untriaged pointer that
    nobody notices.  Measured 2026-09-17 (T26): 47 dangling -> 41 exempt, 0
    unclassified.
    """
    guard = _load_guard()
    unclassified = [(r["spec"], r["spec_line"], r["ref"])
                    for r in _dangling_rows(guard)
                    if guard._mention_exemption(r["spec"], r["ref"]) is None]
    assert unclassified == []


def test_repo_index_is_built_from_git_not_from_the_filesystem():
    """The index must not see files git does not track.

    Regression for the defect CI caught on this ticket's first run: the index
    was an `os.walk`, so `.codex/config.toml` -- gitignored by `.gitignore:90`
    and therefore absent from a fresh checkout -- resolved this repo's only
    `config.toml` mention on a developer machine while CI reported it dangling.
    One commit, two answers.

    Honest limit: this comparison can only FAIL where such a local file exists.
    In CI's fresh checkout an `os.walk` returns the same set as git, so this test
    would have passed there even with the bug -- the untriaged-mention test above
    is the one CI sees.
    """
    guard = _load_guard()
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=str(guard.ROOT),
                            capture_output=True)
    if listed.returncode != 0:
        pytest.skip("not a git checkout")
    tracked = {rel for rel in listed.stdout.decode("utf-8").split("\0") if rel}
    indexed = {p for paths in guard._repo_index().values() for p in paths}
    assert sorted(indexed - tracked) == [], "index sees files git does not track"
