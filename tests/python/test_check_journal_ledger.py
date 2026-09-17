"""Synthetic-corpus tests for scripts/check-journal-ledger.py (#676).

The ledger grew 39 placeholder lines because nothing read it.  This harness is
the half that keeps the new gate honest, and it is built around the two ways
this particular check can lie:

* **a substring criterion is self-referentially red** -- the sessions that
  discuss the placeholders quote them in prose, so a substring gate fails on a
  clean ledger (measured 1/3/4 false positives, #676).  `test_substring_*` pins
  the strict criterion against the same bytes rather than trusting the code to
  agree with itself;
* **an index built by walking the filesystem is not the repo** -- #675 slice C
  measured a verdict that depended on a gitignored file.  `test_untracked_*`
  drops a deliberately broken journal into the real working tree and asserts the
  run stays green, which can only hold if the index comes from `git ls-files`.

The last tests run against the real repository: the repo asserts its own state,
and the legacy ledger's counts stay pinned (T26's "a declared count must match
the corpus").

Synthetic corpora monkeypatch `WORKSPACE_DIR` and `iter_journal_paths`, mirroring
`test_check_spec_refs.py`.  `check_skeleton()` is left pointed at the real
skeleton on purpose: it is the tool checking its own declared strings against
the `add_session.py` it ships with.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil

import pytest

GUARD_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "check-journal-ledger.py"
REPO_ROOT = GUARD_PATH.parents[1]
WORKSPACE = REPO_ROOT / ".trellis" / "workspace"

SECTIONS = ("Summary", "Main Changes", "Git Commits", "Testing", "Status", "Next Steps")

DEFAULT_BODY = {
    "Summary": "- did the thing",
    "Main Changes": "- changed a thing",
    "Git Commits": "| Hash | Message |\n|------|---------|\n| `abc1234` | (see git log) |",
    "Testing": "- 11 passed",
    "Status": "[OK] **Completed**",
    "Next Steps": "- nothing pending",
}

PROSE_QUOTING_EVERY_PLACEHOLDER = """\
## 附：占位符清单（本段在**讨论**它们，不是在欠它们）

| 占位符 | 出处 |
|--------|------|
| 骨架的 `- [OK] (Add test results)` | Testing 段 |
| 骨架的 `- None - task complete` | Next Steps 段 |
| 骨架的 `(Add details)` | Main Changes 段 |
| 骨架的 `(Add summary)` | --summary 默认值 |

- 或者写成列表：`- [OK] (Add test results)` / `- None - task complete`
"""


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_journal_ledger_under_test", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def session(number, *, body=None, omit=(), extra=(), title="a session"):
    """One conforming session, with optional omissions / overrides / extras."""
    merged = dict(DEFAULT_BODY)
    merged.update(body or {})
    parts = [f"## Session {number}: {title}", "", "**Date**: 2026-09-17", ""]
    for name in SECTIONS:
        if name in omit:
            continue
        parts += [f"### {name}", "", merged[name], ""]
    for chunk in extra:
        parts += [chunk, ""]
    return "\n".join(parts)


def journal(*sessions):
    return "\n".join(sessions) + "\n"


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """The guard pointed at a synthetic workspace; returns `(guard, write)`."""
    guard = _load_guard()
    base = tmp_path / "workspace"
    (base / "dev").mkdir(parents=True)

    def write(files):
        """Write bytes, not text: `Path.write_text(newline=...)` is Python 3.10+
        and the CI matrix still includes 3.9, while the default translates
        newlines and would leave a `\\r` on every line.  `crlf=True` writes the
        Windows shape on purpose."""
        for rel, spec in files.items():
            if isinstance(spec, str):
                spec = {"body": spec}
            body = spec["body"].replace("\n", spec.get("newline", "\n"))
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode("utf-8"))

    monkeypatch.setattr(guard, "WORKSPACE_DIR", base)
    monkeypatch.setattr(
        guard, "iter_journal_paths",
        lambda workspace_dir=None: (sorted(base.glob("*/journal-*.md")), None),
    )
    return guard, write


def run(guard, capsys):
    """Run the guard, return `(exit_code, stdout)`."""
    code = guard.main([])
    return code, capsys.readouterr().out


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_clean_synthetic_corpus_passes(corpus, capsys):
    guard, write = corpus
    write({"dev/journal-1.md": journal(session(1), session(2))})
    code, out = run(guard, capsys)
    assert code == 0, out
    assert "journal ledger check passed." in out
    assert "2/2 session(s) conforming" in out
    assert "0 placeholder line(s)" in out


# ---------------------------------------------------------------------------
# the criterion itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("marker,section", [
    ("- [OK] (Add test results)", "Testing"),
    ("- None - task complete", "Next Steps"),
    ("(Add details)", "Main Changes"),
    ("(Add summary)", "Summary"),
])
def test_bare_placeholder_line_fails(corpus, capsys, marker, section):
    """The whole-line form is exactly what the skeleton leaves behind."""
    guard, write = corpus
    write({"dev/journal-1.md": journal(session(1, body={section: marker}))})
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "PLACEHOLDER" in out
    assert f"unfilled skeleton placeholder {marker!r}" in out


def test_placeholder_is_reported_with_its_line_number(corpus, capsys):
    guard, write = corpus
    text = journal(session(1, body={"Testing": "- [OK] (Add test results)"}))
    write({"dev/journal-1.md": text})
    expected = text.split("\n").index("- [OK] (Add test results)") + 1
    code, out = run(guard, capsys)
    assert code == 1
    assert f"journal-1.md:{expected}: PLACEHOLDER" in out


def test_prose_quoting_a_placeholder_is_not_a_violation(corpus, capsys):
    """The #676 regression: the session that *documents* the placeholders."""
    guard, write = corpus
    write({"dev/journal-1.md": journal(session(1), PROSE_QUOTING_EVERY_PLACEHOLDER)})
    code, out = run(guard, capsys)
    assert code == 0, out


def test_substring_criterion_would_be_red_on_a_clean_journal(corpus):
    """Absolute pin, computed independently of the guard's own helper.

    The same bytes must give 0 by whole-line equality and >0 by substring, which
    is why the criterion cannot be relaxed: a substring gate fails on a clean
    ledger (1/3/4 measured in #676) and would be switched off.
    """
    guard, _write = corpus
    lines = journal(session(1), PROSE_QUOTING_EVERY_PLACEHOLDER).split("\n")
    loose = {m: sum(1 for x in lines if m in x) for m in guard.DECLARED_PLACEHOLDERS}
    strict = guard.placeholder_lines(lines)
    assert loose["- [OK] (Add test results)"] > 0
    assert loose["- None - task complete"] > 0
    assert loose["(Add details)"] > 0
    assert strict == []


# ---------------------------------------------------------------------------
# structural drift
# ---------------------------------------------------------------------------

def test_missing_standard_heading_fails(corpus, capsys):
    """The S16/17/18 shape: no `### Git Commits` at all."""
    guard, write = corpus
    write({"dev/journal-1.md": journal(session(1, omit=("Git Commits",)))})
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "HEADING_COUNT" in out
    assert "### Git Commits missing" in out


def test_duplicated_heading_fails(corpus, capsys):
    """The leftover-skeleton-tail shape: the real section plus the stub."""
    guard, write = corpus
    tail = "\n".join([
        "### Testing", "", "- [OK] (Add test results)", "",
        "### Status", "", "[OK] **Completed**", "",
    ])
    write({"dev/journal-1.md": journal(session(1, extra=(tail,)))})
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "### Testing x2" in out
    assert "PLACEHOLDER" in out


@pytest.mark.parametrize("broken_heading", ["## Session: a session", "## Session  "])
def test_session_heading_without_a_number_fails(corpus, capsys, broken_heading):
    """Detection must be wider than parsing.

    `## Session: a session` does not match the skeleton's shape.  If the guard
    only recognised well-formed headings, that line would not be a *broken*
    session -- it would be no session at all, and the whole section (placeholders
    included) would go unread.
    """
    guard, write = corpus
    broken = journal(session(1)).replace("## Session 1: a session", broken_heading)
    write({"dev/journal-1.md": broken})
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "SESSION_NUMBER" in out


def test_duplicate_session_number_fails(corpus, capsys):
    guard, write = corpus
    write({
        "dev/journal-1.md": journal(session(7)),
        "dev/journal-2.md": journal(session(7)),
    })
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "session 7 is used twice" in out


# ---------------------------------------------------------------------------
# the empty corpus must not pass
# ---------------------------------------------------------------------------

def test_no_journals_at_all_fails(corpus, capsys):
    guard, _write = corpus
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "EMPTY_CORPUS" in out


def test_journal_without_sessions_fails(corpus, capsys):
    guard, write = corpus
    write({"dev/journal-1.md": "# Journal - Test (Part 1)\n\nno sessions here\n"})
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "EMPTY_CORPUS" in out


def test_index_failure_fails_closed(corpus, capsys, monkeypatch):
    guard, write = corpus
    write({"dev/journal-1.md": journal(session(1))})
    monkeypatch.setattr(guard, "iter_journal_paths", lambda workspace_dir=None: ([], "git is gone"))
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "NO_INDEX" in out


# ---------------------------------------------------------------------------
# line endings
# ---------------------------------------------------------------------------

def test_crlf_journal_is_still_measured(corpus, capsys):
    """A `\\r` left by a CRLF checkout must not hide a placeholder.

    This is the recurring trap (#676 is its third appearance): assert uniform
    CRLF and then match LF anchors, or strip only spaces, and the check silently
    stops matching anything.
    """
    guard, write = corpus
    broken = journal(session(1, body={"Testing": "- [OK] (Add test results)"}))
    write({"dev/journal-1.md": {"body": broken, "newline": "\r\n"}})
    # Pin the *reader*, not just the verdict: the guard is insensitive to `\r`
    # because `read_journal` normalises before anything compares, so a single
    # mutation of the comparison cannot make this test red.  The mutation that
    # can is a reader that leaks `\r` -- so assert the reader's contract.
    path = guard.WORKSPACE_DIR / "dev" / "journal-1.md"
    lines, crlf, lone = guard.read_journal(path)
    assert crlf > 0 and lone == 0
    assert not any("\r" in line for line in lines)
    expected_line = broken.split("\n").index("- [OK] (Add test results)") + 1
    assert guard.placeholder_lines(lines) == [(expected_line, "- [OK] (Add test results)")]
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "PLACEHOLDER" in out


def test_crlf_conforming_journal_passes(corpus, capsys):
    guard, write = corpus
    write({"dev/journal-1.md": {"body": journal(session(1)), "newline": "\r\n"}})
    code, out = run(guard, capsys)
    assert code == 0, out


def test_mixed_line_endings_are_advisory_only(corpus, capsys):
    guard, write = corpus
    mixed = journal(session(1)).replace("- nothing pending\n", "- nothing pending\r\n")
    write({"dev/journal-1.md": mixed})
    code, out = run(guard, capsys)
    assert code == 0, out
    assert "ADVISORY" in out and "mixes line endings" in out


# ---------------------------------------------------------------------------
# the legacy pin table
# ---------------------------------------------------------------------------

def test_legacy_pin_drift_fails(corpus, capsys):
    """T26's criterion: a declared count must match the corpus."""
    guard, write = corpus
    pinned_key = sorted(guard.LEGACY_JOURNALS)[0]  # e.g. 'JameryW/journal-1.md'
    write({pinned_key: journal(session(1))})
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "LEGACY_DRIFT" in out


def test_a_new_file_under_a_legacy_dir_is_judged_not_pinned(corpus, capsys):
    """Only the pinned paths are exempt; a new journal is ours and must be clean."""
    guard, write = corpus
    legacy_dir = sorted(guard.LEGACY_JOURNALS)[0].split("/")[0]
    broken = journal(session(1, body={"Testing": "- [OK] (Add test results)"}))
    write({f"{legacy_dir}/journal-9.md": broken})
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "PLACEHOLDER" in out
    assert "LEGACY_DRIFT" not in out


# ---------------------------------------------------------------------------
# stale skeleton (the check watching itself)
# ---------------------------------------------------------------------------

def test_skeleton_missing_a_heading_is_stale(tmp_path, monkeypatch):
    guard = _load_guard()
    skeleton = tmp_path / "add_session.py"
    skeleton.write_bytes(b"### Summary\n('- None - task complete')\n")
    problems = guard.check_skeleton(skeleton)
    kinds = {v.kind for v in problems}
    assert kinds == {"STALE_SKELETON"}
    assert any("### Testing" in v.detail for v in problems)


def test_skeleton_absent_is_stale(tmp_path):
    guard = _load_guard()
    problems = guard.check_skeleton(tmp_path / "nope.py")
    assert [v.kind for v in problems] == ["STALE_SKELETON"]


def test_stale_skeleton_verdict_reaches_the_run(corpus, capsys, monkeypatch):
    """The two tests above call `check_skeleton` directly, so neither pins that
    `main` actually consults it -- dropping the call would leave them green."""
    guard, write = corpus
    write({"dev/journal-1.md": journal(session(1))})
    monkeypatch.setattr(
        guard, "check_skeleton",
        lambda skeleton=None: [guard.Violation("skeleton", 0, "STALE_SKELETON", "boom")],
    )
    code, out = run(guard, capsys)
    assert code == 1, out
    assert "STALE_SKELETON" in out


# ---------------------------------------------------------------------------
# the real repository asserts its own state
# ---------------------------------------------------------------------------

def test_real_corpus_conforms(capsys):
    guard = _load_guard()
    code, out = run(guard, capsys)
    assert code == 0, out
    assert "journal ledger check passed." in out
    assert "STALE_SKELETON" not in out  # our declared strings still match the real skeleton
    assert "this ledger: 2 file(s)" in out
    assert "legacy (pinned, not fixed here): 3 file(s)" in out


def test_real_clean_ledger_is_red_under_the_substring_criterion(capsys):
    """First-hand: the loose criterion is red on the ledger that just passed.

    Only this ledger's files -- the legacy ledger legitimately holds 363
    placeholders, which is precisely why it is pinned and not judged.
    """
    guard = _load_guard()
    journals, error = guard.iter_journal_paths()
    assert error is None and journals
    ours = [p for p in journals if guard.journal_key(p) not in guard.LEGACY_JOURNALS]
    assert len(ours) == 2  # the T-series ledger, journal-1 / journal-2
    loose = 0
    strict = 0
    for path in ours:
        lines, _crlf, _lone = guard.read_journal(path)
        strict += len(guard.placeholder_lines(lines))
        loose += sum(1 for x in lines for m in guard.DECLARED_PLACEHOLDERS if m in x)
    assert strict == 0
    assert loose > 0


def test_untracked_journal_is_invisible_to_the_index(capsys):
    """The T26 regression, executed for real.

    A deliberately broken journal is dropped into the real working tree without
    being staged.  A filesystem-walking index would read it and fail; `git
    ls-files` cannot see it, so the run stays green and the same commit gives the
    same verdict on a clean CI checkout.
    """
    guard = _load_guard()
    stray_dir = WORKSPACE / "zz-untracked-probe"
    stray = stray_dir / "journal-1.md"
    try:
        stray_dir.mkdir(parents=True, exist_ok=True)
        broken = journal(session(9001, body={"Testing": "- [OK] (Add test results)"}))
        stray.write_bytes(broken.encode("utf-8"))
        assert stray.is_file()  # it really is on disk
        tracked, error = guard.iter_journal_paths()
        assert error is None
        assert all("zz-untracked-probe" not in p.as_posix() for p in tracked)
        code, out = run(guard, capsys)
        assert code == 0, out
        assert "zz-untracked-probe" not in out
    finally:
        shutil.rmtree(stray_dir, ignore_errors=True)
    assert not stray_dir.exists()
