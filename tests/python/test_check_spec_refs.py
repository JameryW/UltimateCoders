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
import re
import subprocess
import sys

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


# --------------------------------------------------------------------------
# Unanchored references (T42 / #692)
#
# A located reference is verifiable only through one of the two advisory
# anchors: the symbol named on the spec line, or the code the line quotes.
# 53 of the 130 references have NEITHER, so their line number is
# unfalsifiable by construction -- measured 2026-09-20: mutating all 60
# (pre-repair) to a different in-range value left the output byte-identical,
# 0/60.
#
# A: the census is reported.  B: the necessary-condition proof -- the
# mutation really is invisible.  C: the positive control -- the same mutation
# IS visible when an anchor exists, so B cannot pass on a no-op mutation.
# D: the caliber is `best is None`, not "no symbol on the line".  E: a
# `path:line` span is a pointer, not quoted code (the false CONTENT_MISMATCH).
# --------------------------------------------------------------------------


def _shape(module):
    """Every field of a reference except the line number itself.

    Both calibers are in here (T43 / #693): a `_shape` comparison is what
    proves a line-number mutation invisible, and it must therefore be able
    to see a change in either one.
    """
    return [(r["spec"], r["spec_line"], r["ref"].split(":")[0], r["verdict"],
             r["symbol"], r["def_line"], r["offset"], r["unanchored"],
             r["line_unchecked"], r["content_candidate_count"])
            for r in module.collect() if r["kind"] == "ref"]


def test_a_located_reference_with_nothing_to_check_is_reported(guard):
    """Ablation A -- the "line names nothing" half of the caliber.

    The span is nothing but a path carrying a line number, so the extractor
    declines it outright (T44/#694 -- before that, what saved this row was the
    directory part defeating IDENT_RE, which is exactly why the bare-basename
    shape behaved differently; contrast D).  A and D together are what
    separates the two candidate calibers: a wrong one spelled "the line
    names a symbol" reds D but leaves A green, while a flag that is never
    set reds both -- measured as M1/M2 in the ablation run.
    """
    _write_files(guard.ROOT, {
        "crates/one/target.py": "import os\ndef run_loop():\n    return 1\n",
        ".trellis/spec/backend/spec.md":
            "see `crates/one/target.py:2` for the loop\n",
    })
    assert guard._symbols_on("see `crates/one/target.py:2` for the loop") == []
    rows = [r for r in guard.collect() if r["kind"] == "ref"]
    assert [r["verdict"] for r in rows] == ["OK"], rows
    assert [r["unanchored"] for r in rows] == [True], rows
    # It is the *silence* that makes it unanchored, not a failure: neither
    # advisory fires for it, which is the whole point of the flag.
    assert [r["content_candidate_count"] for r in rows] == [0], rows
    assert [r["symbol"] for r in rows] == [None], rows


def test_moving_the_line_number_is_invisible_without_an_anchor(guard):
    """Ablation B -- the necessary-condition proof.

    The claim "its line number cannot be verified" is structural only if the
    mutation is genuinely invisible.  Without this, A could be passing on a
    fixture where the guard never reads the line number at all.
    """
    spec = ".trellis/spec/backend/spec.md"
    _write_files(guard.ROOT, {
        "crates/one/worker.py": "import os\ndef run_loop():\n    return 1\n",
        spec: "see `worker.py:2` for the loop\n",
    })
    first = _shape(guard)
    _write_files(guard.ROOT, {spec: "see `worker.py:3` for the loop\n"})
    assert _shape(guard) == first


def test_the_same_mutation_is_visible_once_an_anchor_exists(guard):
    """Ablation C -- the positive control for B.

    Same corpus shape and the same one-token mutation, but the line now names
    a symbol that has a definition.  The reference moves OK -> STALE, so the
    guard does read line numbers and B\'s silence is about the missing anchor.
    Real-corpus counterpart (measured 2026-09-20): the same mutation moved the
    stale count 7 -> 6.
    """
    spec = ".trellis/spec/backend/spec.md"
    body = ("# one\n# two\n# three\n# four\n# five\n# six\n# seven\n"
            "# eight\n# nine\ndef run_loop():\n    return 1\n# twelve\n")
    _write_files(guard.ROOT, {"crates/one/worker.py": body,
                              spec: "see `worker.py:10-12` (`run_loop`)\n"})
    inside = [(r["verdict"], r["offset"], r["unanchored"])
              for r in guard.collect() if r["kind"] == "ref"]
    _write_files(guard.ROOT, {spec: "see `worker.py:2-4` (`run_loop`)\n"})
    outside = [(r["verdict"], r["offset"], r["unanchored"])
               for r in guard.collect() if r["kind"] == "ref"]
    assert inside == [("OK", None, False)], inside
    assert outside == [("STALE", 8, False)], outside


def test_a_symbol_with_no_definition_still_counts_as_unanchored(guard):
    """Ablation D -- the caliber, asserted directly.

    The line DOES name a symbol: `never_defined` is an ordinary identifier, not
    a path token, so the extractor keeps it -- and nothing in the repo defines
    it.  A caliber spelled as "the spec line names no symbol" would report
    False here and silently drop every such reference from the census.

    T44/#694 rewrote this fixture.  It used to rest on a *phantom*: `worker`,
    bitten out of the path token `worker.py:2`, also had no definition.  That is
    precisely the shape this ticket removed, so the distinction has to be built
    from a real identifier now -- which is the more honest fixture anyway, since
    81 of the 130 real reference lines name something with no definition
    anywhere.  Quoting it in bold rather than in backticks is load-bearing: a
    `_`-bearing inline span would also become a *content candidate*, and the
    line would stop being unanchored for an unrelated reason.
    """
    assert guard._symbols_on("see `worker.py:2` for **never_defined**") == [
        "never_defined"]
    _write_files(guard.ROOT, {
        "crates/one/worker.py": "import os\ndef run_loop():\n    return 1\n",
        ".trellis/spec/backend/spec.md":
            "see `worker.py:2` for **never_defined**\n",
    })
    rows = [r for r in guard.collect() if r["kind"] == "ref"]
    assert [r["unanchored"] for r in rows] == [True], rows
    assert [r["symbol"] for r in rows] == [None], rows
    assert [r["content_candidate_count"] for r in rows] == [0], rows


def test_a_path_pointer_is_a_pointer_not_quoted_code(guard):
    """Ablation E -- the false CONTENT_MISMATCH source (#692).

    Both parsers declined this shape for the wrong reason: the suffix test
    sees `.md:134` (not a code extension) and PATH_SPAN_RE did not list `md`,
    so a `path:line` POINTER reached the content-candidate branch as if it
    were quoted code and could never occur in the target.  Real corpus effect:
    `1 of 134 have no matching quoted content`, on `p2-recon.md:67`.
    """
    assert pathlib.PurePath("guide.md:134").suffix == ".md:134"
    assert guard.PATH_SPAN_RE.fullmatch(".trellis/spec/guides/guide.md:134")
    assert guard.PATH_SPAN_RE.fullmatch("crates/one/worker.py:2-4")
    matched, candidates = guard._content_anchor(
        "see `worker.py:2` cf `.trellis/spec/guides/guide.md:134`", "x = 1\n")
    assert candidates == [], candidates
    assert matched is None


def test_a_doc_pointer_on_the_line_does_not_fake_a_content_mismatch(guard):
    """Ablation E, end to end: the advisory itself must stay silent."""
    _write_files(guard.ROOT, {
        "crates/one/worker.py": "import os\ndef run_loop():\n    return 1\n",
        "docs/architecture/note.md":
            "see `crates/one/worker.py:2` as written in `docs/guides/other.md:9`\n",
    })
    rows = [r for r in guard.collect() if r["kind"] == "ref"]
    assert [r["content_candidate_count"] for r in rows] == [0], rows
    assert [r["content_ok"] for r in rows] == [True], rows


def test_real_corpus_unanchored_census_is_reported():
    """The count this ticket is about, measured on the real corpus.

    Moving any of these changes this number in the same commit, which is the
    only thing keeping it from being a comment.  Each repair decrements it:
    60 pre-repair -> 57 (one false content candidate removed) -> 53 (the four
    symbol anchors below).
    """
    guard = _load_guard()
    rows = guard.collect()
    refs = [r for r in rows if r["kind"] == "ref"]
    assert len(refs) == 130, len(refs)
    assert sum(1 for r in refs if r["unanchored"]) == 53
    # 271 -> 275 across the P2-baseline window (re-measured 9be830df -> HEAD
    # on the red-main repair ticket, keyed on spec+line+ref then re-checked by
    # (ref, verdict) multiset to survive P2's +2 line shift in
    # agent-capability-spec.md): +3 from the new runtime-policy-spec.md:5,6,7
    # (p2-policy doc, worker-service spec, agent-capability spec -- all
    # backticked) and +1 from agent-capability-spec.md's new backticked
    # runtime-policy-spec.md link. worker-service-spec.md's two added
    # runtime-policy links are plain markdown (not backticked), so the guard
    # never sees them -- net 0 there. All four are MENTION_RESOLVED, so the
    # unclassified count is untouched.
    assert sum(1 for r in rows if r["kind"] == "mention") == 275


def test_real_corpus_has_no_false_content_mismatch():
    """`1 of 134` -> `0` (T42/#692).

    Honest limit, measured: reverting ONLY the regex no longer reproduces
    the symptom, because this fix's own docstring in the guard quotes the
    example `md:134`, and that substring then satisfies the same content
    test -- the prose that documents the defect masks it.  So this pin is
    reddened by a corpus mutation (a quoted token that stops occurring in
    its target), while the regex revert is pinned by the synthetic
    mechanism test above.
    """
    guard = _load_guard()
    bad = [(r["spec"], r["spec_line"], r["ref"])
           for r in guard.collect() if r["kind"] == "ref" and not r["content_ok"]]
    assert bad == []


def test_real_corpus_repaired_references_carry_their_symbol():
    """The four corpus repairs are live and load-bearing.

    Each row used to be unanchored.  Asserting the symbol AND its definition
    line (not merely "no longer unanchored") pins that the anchor is the one
    the edit meant: a line number that drifts out of the range turns these
    STALE, which is precisely the judgement that did not exist before.
    """
    guard = _load_guard()
    want = {
        (".trellis/spec/backend/database-guidelines.md", 64):
            ("crates/uc-engine/src/memory/short_term.rs", "ShortTermMemory", 45),
        (".trellis/spec/backend/database-guidelines.md", 131):
            ("crates/uc-engine/src/memory/short_term.rs", "list_keys", 270),
        (".trellis/spec/frontend/hook-guidelines.md", 19):
            ("crates/uc-engine/src/events.rs", "AgentEventType", 35),
        (".trellis/spec/frontend/hook-guidelines.md", 109):
            ("python/ultimate_coders/agent/orchestrator.py", "refresh_heartbeat", 158),
    }
    rows = [r for r in guard.collect()
            if r["kind"] == "ref" and (r["spec"], r["spec_line"]) in want]
    got = {(r["spec"], r["spec_line"]): (r["target"], r["symbol"], r["def_line"])
           for r in rows}
    assert got == want
    assert [r["unanchored"] for r in rows] == [False] * 4, rows


def test_real_corpus_line_one_pointers_are_gone():
    """`types.py:1` x4 were pointers at line 1, not references.

    Line 1 of each file is an import, so a `:1` pointer can never be anchored
    and claims nothing checkable.  The four spans lost their line number and
    became mentions, where "the file is gone" is still caught (asserted in
    the type-safety spec: all four resolve as mentions now).
    """
    guard = _load_guard()
    pointers = [(r["spec"], r["ref"]) for r in guard.collect()
                if r["kind"] == "ref" and r["start"] == 1
                and r["ref"].split(":")[0] in {"types.py", "memory.py",
                                             "query.py", "config.py"}]
    assert pointers == []
    resolved = [(r["spec_line"], r["ref"], r["verdict"])
                for r in guard.collect()
                if r["kind"] == "mention"
                and r["spec"] == ".trellis/spec/frontend/type-safety.md"]
    assert (26, "python/ultimate_coders/agent/types.py", "MENTION_RESOLVED") in resolved
    assert (26, "python/ultimate_coders/config.py", "MENTION_RESOLVED") in resolved


# --------------------------------------------------------------------------
# Line-checkable references: `unanchored` is a subset (T43 / #693)
# A: a content anchor alone leaves the line number unchecked -- the shape T42's
#    count excluded while its wording claimed it.  B: the necessary-condition
#    proof for that wider tier.  C: the positive control, same fixture, one
#    symbol named.  D: the real-corpus census and the nesting between the two
#    calibers.  E: the printed number must be the wider one.
# --------------------------------------------------------------------------


def test_a_content_anchor_alone_leaves_the_line_number_unchecked(guard):
    """Ablation A -- the shape the narrow caliber excluded.

    The spec line quotes `UC_CAP_BROWSER`, which does occur in the target, so
    `content_candidate_count == 1` and T42's caliber reports `unanchored` False.
    But `_content_anchor` is a *file-level* substring test, so the line number
    is still compared against nothing: this row has an anchor AND an
    unverifiable line number at the same time.  The quoted token has to be one
    that is NOT also a symbol with a definition -- `run_loop` would become a
    symbol anchor and the row would stop demonstrating anything.
    """
    spec = ".trellis/spec/backend/spec.md"
    _write_files(guard.ROOT, {
        "crates/one/worker.py": 'import os\n\n\n'
                                'def run_loop():\n'
                                '    return UC_CAP_BROWSER\n',
        spec: "see `worker.py:2` for `UC_CAP_BROWSER`\n",
    })
    rows = [r for r in guard.collect() if r["kind"] == "ref"]
    assert [(r["verdict"], r["content_candidate_count"], r["content_ok"],
             r["symbol"], r["unanchored"], r["line_unchecked"])
            for r in rows] == [("OK", 1, True, None, False, True)], rows


def test_moving_the_line_number_stays_invisible_with_only_a_content_anchor(guard):
    """Ablation B -- the necessary-condition proof for the wider tier.

    Same fixture shape and same one-token mutation as T42's test B, but the line
    also quotes code that is present.  T42's caliber calls this row anchored;
    the mutation is nevertheless invisible, which is the whole reason the
    reported number had to change.  Without this, `line_unchecked` could be a
    flag that is merely set more often.
    """
    spec = ".trellis/spec/backend/spec.md"
    _write_files(guard.ROOT, {
        "crates/one/worker.py": 'import os\n\n\n'
                                'def run_loop():\n'
                                '    return UC_CAP_BROWSER\n',
        spec: "see `worker.py:5` for `UC_CAP_BROWSER`\n",
    })
    first = _shape(guard)
    _write_files(guard.ROOT, {spec: "see `worker.py:4` for `UC_CAP_BROWSER`\n"})
    assert _shape(guard) == first


def test_the_same_fixture_is_visible_once_a_symbol_is_named(guard):
    """Ablation C -- the positive control for B, on the identical corpus.

    Only the spec line differs: it now names `run_loop`, whose definition line
    the target contains.  The identical mutation moves the row OK -> STALE, so
    B's silence is about the missing anchor and not about a dead mutation.
    """
    spec = ".trellis/spec/backend/spec.md"
    _write_files(guard.ROOT, {
        "crates/one/worker.py": 'import os\n\n\n'
                                'def run_loop():\n'
                                '    return UC_CAP_BROWSER\n',
        spec: "see `worker.py:4` (`run_loop`) for `UC_CAP_BROWSER`\n",
    })
    inside = [(r["verdict"], r["offset"], r["unanchored"], r["line_unchecked"])
              for r in guard.collect() if r["kind"] == "ref"]
    _write_files(guard.ROOT,
                 {spec: "see `worker.py:5` (`run_loop`) for `UC_CAP_BROWSER`\n"})
    outside = [(r["verdict"], r["offset"], r["unanchored"], r["line_unchecked"])
               for r in guard.collect() if r["kind"] == "ref"]
    assert inside == [("OK", None, False, False)], inside
    assert outside == [("STALE", -1, False, False)], outside


def test_real_corpus_line_unchecked_census_is_reported():
    """The number this ticket is about, measured on the real corpus.

    T42 reported 53 of 130 under a sentence that read "so a changed line number
    cannot be detected".  53 is only the tier with no anchor at all; the
    property belongs to the 104 with no *positional* anchor, and only the 17
    with a symbol anchor have a checked line number.  Moving any of these
    numbers is a same-commit change.
    """
    guard = _load_guard()
    refs = [r for r in guard.collect() if r["kind"] == "ref"]
    located = [r for r in refs if r["verdict"] in ("OK", "STALE")]
    assert len(refs) == 130, len(refs)
    assert len(located) == 121, len(located)
    assert sum(1 for r in located if r["line_unchecked"]) == 104
    assert sum(1 for r in located if r["unanchored"]) == 53
    assert sum(1 for r in located if r["symbol"] is not None) == 17


def test_the_two_calibers_are_nested_and_their_difference_is_content_only():
    """The nesting, plus the difference pinned by its *defining* property.

    "The sets differ" alone would pass for any pair; the subset direction alone
    would pass if `unanchored` were empty; a bare count alone would pass for a
    wrong predicate that happens to move the same number of rows.  So the 51
    are pinned by what makes them the difference: no symbol anchor, at least one
    content candidate, verdict OK.
    """
    guard = _load_guard()
    located = [r for r in guard.collect()
               if r["kind"] == "ref" and r["verdict"] in ("OK", "STALE")]
    leaked = [(r["spec"], r["spec_line"]) for r in located
              if r["unanchored"] and not r["line_unchecked"]]
    assert leaked == [], leaked
    diff = [r for r in located if r["line_unchecked"] and not r["unanchored"]]
    assert len(diff) == 51, len(diff)
    assert [(r["symbol"], r["content_candidate_count"] > 0, r["verdict"])
            for r in diff] == [(None, True, "OK")] * len(diff)


def _run_guard(*args):
    """The guard as CI runs it, so the wording comes from the real entry
    point rather than from a helper that could drift from it."""
    proc = subprocess.run([sys.executable, str(GUARD_PATH), *args],
                          cwd=str(GUARD_PATH.parents[1]),
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_the_summary_number_is_the_wider_one():
    """Ablation E1 -- the defect WAS a wording over-claim, so pin the number.

    T42's summary printed 53 next to "so a changed line number cannot be
    detected".  The number beside that claim is now 104 of 121 located.
    """
    out = _run_guard()
    claim = [line.strip() for line in out.splitlines()
             if "unchecked line number" in line]
    assert len(claim) == 2, claim          # the advisory and the summary line
    assert claim[1].startswith("104 of 121"), claim[1]
    assert "have NO checkable anchor" not in out


def test_the_default_advisory_reports_the_wider_number():
    """Ablation E2 -- the advisory line is separate code from the summary.

    It is the half a reader sees without `--audit`, and it must carry both
    numbers: the property and the tier.
    """
    out = _run_guard()
    advisory = [line for line in out.splitlines()
                if line.startswith("ADVISORY:")
                and "unchecked line number" in line]
    assert len(advisory) == 1, advisory
    assert advisory[0].startswith("ADVISORY: 104 of 121"), advisory[0]
    assert "(53 of them have no anchor at all" in advisory[0], advisory[0]


def test_the_audit_list_marks_the_anchor_free_tier():
    """Ablation E3 -- one row per counted reference, with the 53 marked.

    Counting the rows pins that the list and the count have one source;
    counting the marks pins that the narrower tier is still visible inside
    the wider one.
    """
    out = _run_guard("--audit")
    head = "have an UNCHECKED LINE NUMBER"
    assert out.count(head) == 1
    rest = out.split(head, 1)[1].split("\n", 1)[1]
    rows = [line for line in rest.split("ADVISORY", 1)[0].splitlines()
            if line.strip()]
    assert len(rows) == 104, len(rows)
    assert sum(1 for line in rows if "<- no anchor at all" in line) == 53


def test_an_ambiguous_row_is_never_anchored(guard):
    """The flags are located-only by construction, and that is pinned HERE.

    An AMBIGUOUS row is the only shape that could have leaked into the
    census, and it cannot: the anchor block sits inside `structural is
    None`, and a `suffix_ambiguous` row skips it entirely.  So such a row
    has `content_candidate_count` 0 even though its line quotes code (which
    also means the quoted-code-absent advisory cannot fire for it) and both
    flags are False.  T43/#693 removed the explicit `and verdict in {\"OK\",
    \"STALE\"}` fence T42 had put on both fields, after measuring it to be a
    no-op -- it could not fire, because the assignment it guards is already
    unreachable from an AMBIGUOUS verdict.  This test keeps that construction
    from regressing.
    """
    _write_files(guard.ROOT, {
        "crates/one/dup.py": "SOME_TOKEN = 1\n",
        "crates/two/dup.py": "SOME_TOKEN = 2\n",
        ALPHA_PATH: "see `dup.py:1` for `SOME_TOKEN`\n",
    })
    rows = [r for r in guard.collect() if r["kind"] == "ref"]
    assert [(r["verdict"], r["content_candidate_count"], r["content_ok"],
             r["unanchored"], r["line_unchecked"]) for r in rows] == [
        ("AMBIGUOUS", 0, True, False, False)], rows


# --------------------------------------------------------------------------
# One "is this a path?" question, one predicate (T44 / #694)
#
# `_symbols_on` used to ask `suffix in CODE_EXT`, which a `path:line` span
# defeats -- `PurePath("worker.py:524").suffix` is `.py:524`, not a code
# extension -- so the span survived and the split produced `worker`, `types`,
# `step_condition` ... as PHANTOM symbols named after files.  `_content_anchor`
# asked `PATH_SPAN_RE or suffix in CODE_EXT`, and T42 had already taught
# PATH_SPAN_RE the `path[:line]` shape.  Two answers to one question.
#
# Measured on the pre-fix tree (`5ee7be2`): 89 of 130 reference lines carried a
# phantom, 115-122 (row, word, target) triples depending on the calibre, and 0
# of them was a definition in its own target.  That is why no VERDICT moves --
# and why a verdict-level test here would have been vacuous, on the fixed code
# and on the broken code alike.
#
# A: the predicate is one function.  B: the bite is gone, and a mixed span
# keeps its real anchor.  C: the two consumers agree -- with the non-emptiness
# assertion that makes agreement mean something.  D: the corpus moves exactly
# where predicted and nowhere else.  E: the hazard tripwire, computed without
# the fixed extractor.
# --------------------------------------------------------------------------


def test_the_path_predicate_is_one_function():
    """Ablation A -- one predicate, two consumers, or they drift apart again.

    Structural rather than behavioural, and deliberately so: the behavioural
    difference is invisible on the corpus (no phantom ever became a `best`), so
    a test that only compared outcomes would pass against the broken code.
    T42 is the precedent -- it fixed the content-anchor side, wrote the reason
    down, and left this side asking the narrower question.
    """
    source = GUARD_PATH.read_text(encoding="utf-8")
    assert source.count("def _is_path_span(span: str) -> bool:") == 1
    # Each half of the question is stated exactly once, inside it.  These
    # anchors are the *code* forms on purpose: the prose form
    # `PATH_SPAN_RE.match(span)` also occurs in the predicate's own docstring,
    # so the first version of this test counted 2 and failed on its own prose.
    assert source.count("pathlib.PurePath(span).suffix in CODE_EXT") == 1
    assert source.count("return bool(PATH_SPAN_RE.match(span)) or") == 1
    assert source.count("if _is_path_span(span):") >= 2


def test_the_predicate_keeps_both_halves_of_the_question(guard):
    """Ablation A2 -- the `path[:line]` half is what T42 contributed.

    Dropping `PATH_SPAN_RE` from the shared predicate (M2) leaves the suffix
    test, which is exactly the narrower question that caused the defect; these
    two shapes are the ones that separate them.  `.py:524` is not a code
    extension, which is the whole mechanism.
    """
    assert pathlib.PurePath("worker.py:524").suffix == ".py:524"
    assert pathlib.PurePath("worker.py:524").suffix not in guard.CODE_EXT
    assert guard._is_path_span("worker.py:524")
    assert guard._is_path_span("crates/one/worker.py:2-4")
    assert guard._is_path_span("guide.md:134")
    assert guard._is_path_span("docs/architecture/note.md")
    # ...and it is not so wide that a real anchor is swallowed
    assert not guard._is_path_span("Task.to_dict")
    assert not guard._is_path_span("prev.summary")


def test_a_path_pointer_yields_no_symbol_at_all(guard):
    """Ablation B -- the bite itself, at the unit level.

    Four `path:line` shapes that used to yield a word named after the file,
    plus the bare basename (already handled before T44), plus the mixed span
    that must NOT lose its real anchor: only the path token's own words go.
    """
    for shape in ["worker.py:524", "sandbox.py:354", "step_condition.py:1-26",
                  "worker.py:1487-1500", "worker.py"]:
        assert guard._symbols_on(f"see `{shape}`") == [], shape
    assert guard._symbols_on("`Task.to_dict` (`types.py:67-80`)") == [
        "Task", "to_dict"]


def test_both_consumers_agree_on_a_shape_table(guard):
    """Ablation C -- agreement, plus the non-emptiness that makes it mean
    something.

    "The two agree" is satisfied by two implementations that both decline
    everything, so the table has to contain shapes that are not paths and the
    symbol extractor has to actually return something for them.  Every shape
    carries one of `_` `.` `=` `"` -- the pre-filter `_content_anchor` applies
    before the question is ever asked -- so this tests the predicate rather
    than the pre-filter.
    """
    shapes = ["worker.py:524", "crates/one/worker.py:2-4", "guide.md:134",
              "types.py", "Task.to_dict", "prev.files.contains",
              "abort_on_failure=True", 'prev.summary.contains("text")']
    kept: list[str] = []
    for shape in shapes:
        line = f"see `{shape}`"
        by_symbols = guard._symbols_on(line) == []
        _, candidates = guard._content_anchor(line, "nothing here\n")
        by_content = candidates == []
        assert by_symbols == by_content, (shape, by_symbols, by_content)
        if not by_symbols:
            kept.append(shape)
    assert len(kept) >= 4, kept


def _corpus_calibers(module):
    """(lines that name anything, of those, lines that anchor nothing).

    The guard uses the spec line but never exposes its text, so this re-reads
    it the way `collect()` does (`split("\\n")`, index `spec_line - 1`).
    """
    cache: dict[str, list[str]] = {}
    named = diverging = 0
    for row in module.collect():
        if row["kind"] != "ref":
            continue
        path = row["spec"]
        if path not in cache:
            cache[path] = (module.ROOT / path).read_text(
                encoding="utf-8", errors="replace").split("\n")
        if not module._symbols_on(cache[path][row["spec_line"] - 1]):
            continue
        named += 1
        if row["symbol"] is None:
            diverging += 1
    return named, diverging


def test_real_corpus_moves_only_where_predicted():
    """Ablation D -- the corpus effect, which is NOT in the verdicts.

    Removing the phantoms cannot move a verdict: `best` needs a definition and
    none of the phantom words ever was one.  What it moves is the answer to
    "does this line name anything at all" -- 115 -> 98 -- and the divergence
    from the "the line names no symbol" caliber, 98 -> 81.  The 17 rows that
    stop diverging are the ones whose only named words were phantoms, i.e. the
    17 that made T43's stated reason for its caliber load-bearing at all.
    """
    guard = _load_guard()
    rows = [r for r in guard.collect() if r["kind"] == "ref"]
    assert len(rows) == 130, len(rows)
    assert _corpus_calibers(guard) == (98, 81)
    # and the sides this ticket must NOT move, asserted in the same breath
    # (OK / STALE deliberately NOT moved on the red-main repair ticket: the
    # pushed head transiently measured 113 / 8 -- exactly one flip,
    # durable-runtime-p2-recon.md:38 dispatch_gate OK -> STALE, off +7 with
    # the def at worker_service.rs:326 after P2's placement_policy lines --
    # and this same change repairs that pointer to :326 per the T42/T43 rule,
    # re-measured back to 114 / 7. Pinning the transient 113 / 8 alongside
    # the repair would be the blind bump this discipline exists to prevent.)
    assert sum(1 for r in rows if r["verdict"] == "OK") == 114
    assert sum(1 for r in rows if r["verdict"] == "STALE") == 7
    assert sum(1 for r in rows if r["verdict"] == "AMBIGUOUS") == 9
    assert sum(1 for r in rows if r["symbol"] is not None) == 17
    assert sum(1 for r in rows if r["unanchored"]) == 53
    assert sum(1 for r in rows if r["line_unchecked"]) == 104


_PATH_TOKEN_RE = re.compile(
    r"[\w./\\:-]+\.(?:py|rs|ts|tsx|js|jsx|proto|toml|yml|yaml|json|sql|sh|md)"
    r"(?::\d+(?:\s*[-\u2013]\s*\d+)?)?")


def _path_words(module, line):
    """Words a naive extractor takes out of a path token on this line.

    Deliberately NOT `module._symbols_on`: after T44 that function declines
    these spans outright, so a tripwire computed from it would be empty by
    construction and could never fire.

    The calibre is the WIDEST of the three measured on the pre-fix tree: every
    span source `_symbols_on` reads (inline code, bold, heading), the path
    token matched *anywhere* inside the span rather than only when the span is
    nothing but a path, and no subtraction of words that are also named
    legitimately elsewhere on the line.  Measured: 122 triples here, against
    115 under "the span is nothing but a path" (the pre-fix extractor's own
    answer) and 115 under "strip the path spans, subtract what remains".  Those
    two agree *in total* while differing in composition by five rows each way,
    so that agreement is a coincidence -- and a tripwire wants the wider one.
    """
    spans = re.findall(r"`([^`\n]+)`", line)
    spans += re.findall(r"\*\*([^*\n]+)\*\*", line)
    heading = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*$", line)
    if heading:
        spans.append(heading.group(1))
    words: list[str] = []
    for span in spans:
        for token in _PATH_TOKEN_RE.findall(span.strip()):
            for part in re.split(r"[.\s()\[\],=:]+", token):
                if (len(part) > 3 and module.IDENT_RE.fullmatch(part)
                        and part not in module.SYMBOL_STOPWORDS
                        and part not in words):
                    words.append(part)
    return words


def test_no_path_word_is_ever_a_definition_in_its_own_target():
    """Ablation E -- the tripwire for the hazard this ticket defuses.

    The phantom words were harmless for a *contingent* reason: none of them
    happened to be a definition in its own target file.  That is one `def` away
    from breaking -- injecting `def worker` into `worker.py` turns 28 rows from
    OK into a false STALE -- so the precondition is what gets pinned, and it
    has to be computed without the fixed extractor or it would be vacuous.

    Not self-referential: `_definitions` is read from the target body the same
    way `collect()` reads it, and the positive control is a word that IS a
    definition (`SubtaskResult` in `agent.rs`), so a `_definitions` that
    returned nothing would fail this test rather than pass it.
    """
    guard = _load_guard()
    control = guard._definitions(
        (guard.ROOT / "crates/uc-types/src/agent.rs").read_bytes().decode(
            "utf-8", "replace").split("\n"))
    assert "SubtaskResult" in control, sorted(control)[:5]

    cache: dict[str, list[str]] = {}
    triples: list[tuple[str, str, bool]] = []
    for row in guard.collect():
        if row["kind"] != "ref" or not row["target"]:
            continue
        path = row["spec"]
        if path not in cache:
            cache[path] = (guard.ROOT / path).read_text(
                encoding="utf-8", errors="replace").split("\n")
        words = _path_words(guard, cache[path][row["spec_line"] - 1])
        if not words:
            continue
        definitions = guard._definitions(
            (guard.ROOT / row["target"]).read_bytes().decode(
                "utf-8", "replace").split("\n"))
        triples.extend((word, row["target"], word in definitions)
                       for word in words)
    assert len(triples) == 122, len(triples)      # measured, and non-empty
    hits = [(word, target) for word, target, hit in triples if hit]
    assert hits == [], hits
