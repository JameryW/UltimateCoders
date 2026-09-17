"""Synthetic-corpus tests for scripts/check-spec-refs.py (#675 slice C).

`scripts/**` is in no CI workflow's `paths`, so this guard (and its exemption
tables) could rot without anything noticing.  These tests pin the mechanism that
stops the tables from becoming a silent blanket, and
`.github/workflows/ci-scripts.yml` runs them whenever `scripts/**` or
`.trellis/spec/**` changes.

Three properties of the guard shape this harness:

* its filename has a hyphen, so it cannot be imported by name -- load it with
  ``importlib.util.spec_from_file_location``;
* ``ROOT`` / ``SPEC_DIR`` / ``_SPEC_PREFIX`` are module-level constants read at
  call time, so a synthetic corpus is built by monkeypatching those three (plus
  clearing ``_ACK_CACHE``, which memoises the per-spec banner reads);
* it has no import-time side effects (the scan runs under
  ``if __name__ == "__main__"``), so importing it is safe.

The last two tests run against the *real* repo.  They are the enforceable half
of the decision made in #675 slice C: the dangling *count* stays advisory (a
mention is not a reference), but the repo asserts its own state -- every
dangling mention is either fixed or carries a documented reason.
"""

from __future__ import annotations

import importlib.util
import pathlib

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
    module.SPEC_DIR = tmp_path / ".trellis" / "spec"
    module._SPEC_PREFIX = ".trellis/spec/"
    module._ACK_CACHE.clear()
    module.MENTION_EXEMPT = ()
    module.SUBJECT_REMOVED = {}
    module.SPEC_DIR.mkdir(parents=True, exist_ok=True)
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
    nobody notices.  Measured at the slice-B close (2026-09-17): 47 dangling ->
    40 exempt, 0 unclassified.
    """
    guard = _load_guard()
    unclassified = [(r["spec"], r["spec_line"], r["ref"])
                    for r in _dangling_rows(guard)
                    if guard._mention_exemption(r["spec"], r["ref"]) is None]
    assert unclassified == []
