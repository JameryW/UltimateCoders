"""Tests for scripts/check-line-endings.py.

T40 / #690.  Five tracked files had working-tree copies that mixed CRLF and lone
LF (7149 lone LFs in total) while nothing in the repo noticed.  The detector
already existed -- `check-journal-ledger.py` computes `crlf` and `lone_lf` for
every journal -- but it is scoped to journals and explicitly "printed, never a
verdict".  The gap was scope and armament, not knowledge.

MEASURED, NOT ASSUMED
---------------------
"git cannot see line endings" is only half the story, and the first half is
misleading.  Measured on the five files:

  * `git diff` / `git diff --cached` are empty for a line-ending-only residue --
    the clean filter normalises both sides, so the CONTENT layer can never see it.
  * `git status --porcelain` reported the mixed files as CLEAN (their stat cache
    matched).  After normalising them it reported ` M` -- with `git diff` still
    empty, `git diff-files --raw` showing an all-zero destination sha, and
    `git update-index --refresh` saying "needs update".  One `git add` then staged
    nothing and cleared the report.

So `git status` carries no information about line endings in either direction, and
"the working tree is clean" is not a content judgment.  `test_real_repo_is_reconciled`
therefore pins the guard, not `git status`.

WHY THIS IS NOT DECORATION
--------------------------
`check-tasks-refs-selftest.py` records that a CRLF mutation there was attempted
twice and removed twice -- that guard is EOL-robust by construction, so there was
nothing to pin.  This guard is the opposite: the property provably failed on five
real files, and every judgment below is reddened by something.

Sandbox fixtures deliberately include a binary file (with a NUL byte, and CRLF
bytes inside it -- exactly the false positive the classifier prevents) and a
gitlink, so no branch of `tracked_files()` or `is_binary()` is left unreached.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "check-line-endings.py"
PY = sys.executable

# Every message the guard can report.  J1 is a family: a scan that saw nothing is
# not a pass, and a classifier that stopped classifying would report PNG bytes as
# mixed line endings, so both halves are pinned separately.
JUDGMENTS = {
    "non-vacuity-files": "tracked file(s) (expected >=",
    "non-vacuity-binary": "no binary file was skipped",
    "index-mixed": "index blob mixes line endings",
    "worktree-mixed": "working tree mixes line endings",
}

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\r\n\x00"
GITLINK_SHA = "a" * 40


def git(sb: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(sb), *args], capture_output=True)


def build_sandbox(tmp_path: pathlib.Path, name: str = "sb") -> pathlib.Path:
    """A standalone git repo shaped like the real one, with the guard inside it.

    The guard derives its root from `__file__/../..`, so copying the script to
    `<sandbox>/scripts/` is all it takes -- there is no test-only hook in the
    production code.  `core.autocrlf` is forced off so the sandbox has one
    meaning on every machine.
    """
    sb = tmp_path / name
    (sb / "scripts").mkdir(parents=True)
    shutil.copy2(GUARD, sb / "scripts" / GUARD.name)
    assert git(sb, "init", "-q").returncode == 0
    git(sb, "config", "core.autocrlf", "false")

    (sb / "docs").mkdir()
    (sb / "docs" / "readme.md").write_bytes(b"# hi\n\nbody\n")
    (sb / "docs" / "mixed.txt").write_bytes(b"a\nb\n")
    (sb / "assets").mkdir()
    (sb / "assets" / "logo.png").write_bytes(PNG_BYTES)
    assert git(sb, "add", "-A").returncode == 0
    # A gitlink is a commit, not a blob, so `:<path>` cannot read it as content.
    r = git(sb, "update-index", "--add", "--cacheinfo",
            f"160000,{GITLINK_SHA},vendor/dep")
    assert r.returncode == 0, r.stderr
    return sb


def run(sb: pathlib.Path) -> tuple[int, str]:
    r = subprocess.run([PY, str(sb / "scripts" / GUARD.name)],
                       capture_output=True, cwd=str(sb))
    return r.returncode, r.stdout.decode("utf-8", "replace")


def problems(out: str) -> list[str]:
    return [line[len("FAIL: "):].strip() for line in out.splitlines()
            if line.startswith("FAIL: ")]


def make_worktree_mixed(sb: pathlib.Path, rel: str) -> None:
    """Overwrite the WORKING TREE copy with mixed endings, leaving the index LF.

    The file is deliberately NOT re-added.  With `core.autocrlf=false` (the sandbox
    setting, so it means the same thing on every machine) `git add` copies bytes
    verbatim, which would put a mixed blob in the index too and redden J2 as well --
    destroying the independence of the two judgments.  With `core.autocrlf=true`
    `git add` would normalise and hide the very mixture under test.
    """
    p = sb / rel
    assert p.is_file(), f"{rel} must already be tracked"
    blob_before = git(sb, "cat-file", "blob", f":{rel}").stdout
    assert b"\r" not in blob_before, f"{rel}: the fixture should start LF-only"
    p.write_bytes(b"one\r\ntwo\nthree\r\n")
    assert git(sb, "cat-file", "blob", f":{rel}").stdout == blob_before, (
        f"{rel}: the index blob must be untouched"
    )


def inject_mixed_index_blob(sb: pathlib.Path, rel: str) -> None:
    """Put a MIXED blob straight into the index, bypassing the clean filter.

    This is the only way to prove judgment J2 can fail: `git add` would normalise
    a mixed working-tree file to LF, so a mixed index blob cannot be produced the
    ordinary way.
    """
    blob = b"alpha\r\nbeta\ngamma\r\n"
    sha = subprocess.run(["git", "-C", str(sb), "hash-object", "-w", "--stdin"],
                         input=blob, capture_output=True, check=True).stdout.decode().strip()
    r = git(sb, "update-index", "--add", "--cacheinfo", f"100644,{sha},{rel}")
    assert r.returncode == 0, r.stderr


def test_real_repo_is_reconciled() -> None:
    """The guard passes on the real repo, and says what it looked at.

    The two PROPERTIES are pinned exactly.  `binary skipped: 9` is the classifier's
    verdict on a fixed asset set (7 PNG + 2 MP4 -- the same nine git calls `-text`)
    and `gitlink(s) skipped: 1` is the vendored submodule.  Neither moves when a
    ticket adds text files, so they are the honest non-vacuity pin.

    The file COUNTS do move -- every ticket adds tracked files -- so they are pinned
    as the current ticket's pair of reachable states, the way
    `test_check_tasks_refs.py` pins its corpus: T41 added no tracked file in its
    implementation commit, so 1840/1831 is its pre-archive state as well as T40's
    post-archive one, and its archive commit -- four task files -- moved it to
    1844/1835.  T42 added no tracked file in ITS implementation commit (the guard,
    its test and three spec files are all modifications), so 1844/1835 is its
    pre-archive state too, and T42's archive commit -- four task files -- moved it to
    1848/1839.  T43 is that shape a third time: no tracked file in its
    implementation commit (the guard, its test and one doc are modifications), so
    1848/1839 is its pre-archive state as well, and its archive commit -- four task
    files -- moves it to 1852/1843, which is T44's pre-archive state as well: T44 adds
    no tracked file in its implementation commit either (the guard and its test are
    both modifications), so its archive commit -- four task files -- moves it to
    1856/1847.

    Repair (red main, 2026-09-23) is that shape a fourth time, but the window holds
    FOUR tickets, not one. The repair edits themselves add no tracked file
    (journal-3.md, the recon doc and the pin test files are all modifications), so
    1887/1878 is the pre-commit state, and committing this ticket's own five task
    files (prd, research notes, implement/check jsonl, task.json -- all text) moves
    it to 1892/1883. The +31 since T44's 1856/1847 is confirmed per added file
    (`git diff --diff-filter=A --name-only d48edaf HEAD` = exactly 31), not read off
    the total: P2's work commit adds 7 (runtime-policy-spec.md, runtime_metrics.rs,
    the runtime_report example + its integration test, review.py,
    test_review_policy.py, durable-runtime-p2-policy.md), the four archive commits
    add 7 + 6 + 5 + 5 task files, and the live-roster work commit adds
    dispatch_live_roster.rs -- 7 + 23 + 1 = 31, all text (binary skipped still 9,
    gitlink(s) still 1, so the non-vacuity pins below are untouched).

    The current index has 1892 tracked blobs: 1883 text and 9 binary, plus one
    gitlink. The Dashboard cleanup adds ten text files and removes two text
    files, giving 1900/1891 after commit. Both reachable pairs keep the binary
    and gitlink classification pinned. Any other count needs investigation.
    """
    r = subprocess.run([PY, str(GUARD)], capture_output=True, cwd=str(REPO))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, f"guard failed on the real repo:\n{out}"
    assert "binary skipped: 9, gitlink(s) skipped: 1" in out, (
        f"the binary/gitlink classification has moved; got:\n{out}"
    )
    m = re.search(r"tracked file\(s\): (\d+), text scanned: (\d+)", out)
    assert m, f"the guard must report how much it looked at; got:\n{out}"
    # Dashboard cleanup deletes the browser TUI page and its test while adding
    # the Dashboard API image, local Compose override, replacement route test,
    # and two event-view files: ten additions minus two deletions.
    assert (int(m.group(1)), int(m.group(2))) in {(1892, 1883), (1900, 1891)}, (
        f"the scan size has moved: {(m.group(1), m.group(2))}; update the pair"
    )
    assert "line endings check passed." in out, out


def test_no_tracked_file_mixes_line_endings() -> None:
    """The five files T40 normalised stay normalised, checked at the byte level.

    Pinned as "these paths are LF-only in the working tree", which is the property
    that was violated -- not as "these paths have these sizes", which would break
    on any unrelated edit.
    """
    targets = [
        "dashboard/index.html",
        "dashboard/src/grpc/engine_pb.ts",
        "packages/uc-orchestrator/src/grpc/engine_pb.ts",
        "tests/python/test_dashboard_metrics.py",
        "tests/python/test_worker_capabilities.py",
    ]
    for rel in targets:
        data = (REPO / rel).read_bytes()
        assert b"\r" not in data, f"{rel}: a CR came back"
    # And they are byte-identical to their index blobs, which is the justification
    # for choosing LF: the index stored LF all along.
    for rel in targets:
        blob = subprocess.run(["git", "-C", str(REPO), "cat-file", "blob", f":{rel}"],
                              capture_output=True, check=True).stdout
        assert blob == (REPO / rel).read_bytes(), f"{rel}: differs from its index blob"


def test_every_judgment_is_pinned_by_a_mutation(tmp_path) -> None:
    sb = build_sandbox(tmp_path)
    rc, out = run(sb)
    assert rc == 0, f"sandbox baseline must be green:\n{out}"
    assert "binary skipped: 1" in out and "gitlink(s) skipped: 1" in out, out

    # A: a working-tree file mixes endings while its index blob is LF.
    make_worktree_mixed(sb, "docs/mixed.txt")
    rc, out = run(sb)
    assert rc != 0, out
    msgs = problems(out)
    assert any(JUDGMENTS["worktree-mixed"] in m and "docs/mixed.txt" in m for m in msgs), msgs
    assert not any(JUDGMENTS["index-mixed"] in m for m in msgs), (
        "a mixed WORKING TREE must not be reported as a mixed INDEX blob"
    )

    # B: a mixed blob in the index, bypassing the clean filter.
    sb_b = build_sandbox(tmp_path, "sb_b")
    inject_mixed_index_blob(sb_b, "docs/readme.md")
    rc, out = run(sb_b)
    assert rc != 0, out
    msgs = problems(out)
    assert any(JUDGMENTS["index-mixed"] in m and "docs/readme.md" in m for m in msgs), msgs
    assert not any(JUDGMENTS["worktree-mixed"] in m for m in msgs), (
        "an LF-only working tree must not be reported as mixed"
    )

    # C: no binary file left to skip -- the classifier silently stopped working.
    sb_c = build_sandbox(tmp_path, "sb_c")
    assert git(sb_c, "rm", "--cached", "-q", "assets/logo.png").returncode == 0
    (sb_c / "assets" / "logo.png").unlink()
    rc, out = run(sb_c)
    assert rc != 0, out
    assert any(JUDGMENTS["non-vacuity-binary"] in m for m in problems(out)), out

    # D: an index that tracks nothing -- a scan of zero files is not a pass.
    sb_d = tmp_path / "sb_d"
    (sb_d / "scripts").mkdir(parents=True)
    shutil.copy2(GUARD, sb_d / "scripts" / GUARD.name)
    assert git(sb_d, "init", "-q").returncode == 0
    rc, out = run(sb_d)
    assert rc != 0, out
    assert any(JUDGMENTS["non-vacuity-files"] in m for m in problems(out)), out


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_binary_classification_has_both_directions() -> None:
    """`is_binary` is what keeps PNG/MP4 bytes out of the line-ending judgments.

    Without a negative control, an `is_binary` that always returned True would skip
    every file and the guard would pass on an empty scan; one that always returned
    False would report the CRLF bytes inside `PNG_BYTES` as a mixed file.  Both
    directions are pinned, and the fixture is the real shape: a PNG header contains
    CRLF and a NUL.
    """
    guard = _load(GUARD, "check_line_endings")
    assert guard.is_binary(PNG_BYTES) is True
    assert guard.is_binary(b"plain text\n") is False
    assert guard.is_binary(b"") is False

    assert guard.endings(b"a\r\nb\r\n") == (2, 0)
    assert guard.endings(b"a\nb\n") == (0, 2)
    assert guard.endings(b"a\r\nb\n") == (1, 1)
    assert guard.endings(PNG_BYTES) == (2, 1)


def test_the_classification_agrees_with_git_eol(tmp_path) -> None:
    """Two implementations of the same contract must agree AND be non-empty.

    `git ls-files --eol` reports its own `i/` and `w/` verdicts; this guard computes
    them from bytes.  Asserting only agreement would be satisfied by two broken
    implementations that both said "uniform" -- T39's lesson -- so the table must
    also contain both verdicts before the comparison means anything.
    """
    sb = build_sandbox(tmp_path)
    make_worktree_mixed(sb, "docs/mixed.txt")
    inject_mixed_index_blob(sb, "docs/readme.md")

    raw = git(sb, "ls-files", "--eol", "-z", "--cached").stdout
    git_view: dict[str, tuple[str, str]] = {}
    for entry in raw.split(b"\x00"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        cols = meta.split()
        assert len(cols) == 3, meta
        git_view[path.decode("utf-8")] = (cols[0].decode(), cols[1].decode())

    # Load the SANDBOX copy: `REPO` is derived from `__file__`, so loading the real
    # repo's script here would enumerate the real repo while the git view below came
    # from the sandbox -- two different corpora compared against each other.
    guard = _load(sb / "scripts" / GUARD.name, "check_line_endings_sandbox")
    paths, gitlinks = guard.tracked_files()
    blobs = guard.index_blobs(paths)
    assert gitlinks == 1, f"the sandbox fixture should carry one gitlink: {gitlinks}"

    verdicts = set()
    for rel in paths:
        cols = git_view.get(rel)
        assert cols is not None, f"{rel}: git ls-files --eol did not see it"
        blob = blobs[rel]
        if blob is None or guard.is_binary(blob):
            assert "-text" in cols[0], f"{rel}: git does not call it binary: {cols}"
            continue
        i_crlf, i_lone = guard.endings(blob)
        mine_i = bool(i_crlf and i_lone)
        if (sb / rel).is_file():
            disk = (sb / rel).read_bytes()
            if not guard.is_binary(disk):
                w_crlf, w_lone = guard.endings(disk)
                mine_w = bool(w_crlf and w_lone)
            else:
                continue
        else:
            continue
        verdicts |= {mine_i, mine_w}
        assert mine_i is (cols[0] == "i/mixed"), (
            f"{rel}: index verdict {cols[0]} but bytes say mixed={mine_i}")
        assert mine_w is (cols[1] == "w/mixed"), (
            f"{rel}: worktree verdict {cols[1]} but bytes say mixed={mine_w}")

    assert verdicts == {True, False}, f"the table is vacuous: only {verdicts}"
