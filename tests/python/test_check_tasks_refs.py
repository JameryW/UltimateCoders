"""`check-tasks-refs.py` -- the guard for `.trellis` citations in task jsonl.

Context (#680, the C half of #678's "A + C").  `.trellis/tasks/**/*.jsonl`
carries each task's context as `{"file": ".trellis/..."}`.  Nothing covered
those paths: `check-spec-refs.py` indexes `.trellis/spec/` (plus `docs/` since
T41 / #691) and never mentions `jsonl` or `.trellis/tasks`.  So when `archive_task_dir` moved a task
directory, the task's citations to its own files stopped resolving and no
checker said a word -- that corpus is what #678 measured.

Harness shape, and why it is a real subprocess: the guard's whole job is to
decide a verdict from a working tree, and its index comes from `git ls-files`.
A synthetic repo in `tmp_path` with a real `git init` is therefore the only
harness that exercises the code path CI exercises.  The real
`.trellis/tasks/` tree is never mutated; the real corpus gets its own
read-only test at the bottom.

Exit-code contract under test (asserted, not implied):
  * resolve / no problems  -> 0
  * any DANGLING           -> non-zero  (it is repairable; #678 fixed the cause)
  * any MALFORMED          -> non-zero  (a silently-skipped bad line is exactly
                                          where this defect class hides)
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "check-tasks-refs.py"

TASK = "01-01-demo"


def _git_init(root: pathlib.Path) -> None:
    """A real repository: the guard's index is `git ls-files` output."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    for argv in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
        ["config", "core.autocrlf", "false"],
    ):
        subprocess.run(["git"] + argv, cwd=str(root), env=env, check=True,
                       capture_output=True)
    (root / ".gitignore").write_bytes(b"")
    (root / "tracked.txt").write_bytes(b"tracked\n")
    subprocess.run(["git", "add", "-A"], cwd=str(root), env=env, check=True,
                   capture_output=True)


def _jsonl(root: pathlib.Path, rel: str, entries: list[object]) -> pathlib.Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        (e if isinstance(e, str) else json.dumps(e)) + "\n" for e in entries
    )
    path.write_bytes(body.encode("utf-8"))
    return path


def _run(root: pathlib.Path, *argv: str) -> subprocess.CompletedProcess:
    """Run the guard against `root`. Bytes, not `text=True`: on Windows
    `text=True` rewrites `\\n` to `\\r\\n` on the way in, which is how a path
    comparison can mismatch while still exiting 0 -- a trap this repository has
    already paid for."""
    return subprocess.run(
        [sys.executable, str(GUARD), "--root", str(root)] + list(argv),
        cwd=str(root),
        env=dict(os.environ),
        capture_output=True,
    )


def _commit(root: pathlib.Path, msg: str = "c") -> None:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    subprocess.run(["git", "add", "-A"], cwd=str(root), env=env, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=str(root), env=env,
                   check=True, capture_output=True)


def _load_guard():
    """Import the guard as a module (it is a script, so load it by path)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_tasks_refs", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_resolving_citations_pass(tmp_path):
    _git_init(tmp_path)
    (tmp_path / ".trellis" / "tasks" / TASK).mkdir(parents=True)
    (tmp_path / ".trellis" / "tasks" / TASK / "prd.md").write_bytes(b"# t\n")
    _jsonl(
        tmp_path,
        f".trellis/tasks/{TASK}/implement.jsonl",
        [
            {"file": f".trellis/tasks/{TASK}/prd.md", "reason": "requirements"},
            {"file": "tracked.txt", "reason": "not a .trellis ref -- ignored"},
        ],
    )
    _commit(tmp_path)

    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout.decode()
    assert b"1 ok / 0 dangling / 0 malformed" in result.stdout, result.stdout
    assert b"passed" in result.stdout


def test_non_trellis_and_missing_file_key_are_not_counted(tmp_path):
    """The guard's corpus is `.trellis`-prefixed `file` values only. A bare
    filename, a `path` key, or a value-less step entry is not a citation."""
    _git_init(tmp_path)
    task = tmp_path / ".trellis" / "tasks" / TASK
    task.mkdir(parents=True)
    _jsonl(tmp_path, f".trellis/tasks/{TASK}/implement.jsonl", [
        {"file": "tracked.txt", "reason": "bare name"},
        {"path": ".trellis/nope.md", "reason": "wrong key -- not this guard's"},
        {"step": "recon", "detail": "prose mentioning .trellis/tasks/"},
    ])
    _commit(tmp_path)

    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout.decode()
    assert b"scanned 0 " in result.stdout, result.stdout


# --------------------------------------------------------------------------
# DANGLING -- structural, must fail
# --------------------------------------------------------------------------


def test_dangling_citation_fails_and_is_listed(tmp_path):
    _git_init(tmp_path)
    task = tmp_path / ".trellis" / "tasks" / TASK
    task.mkdir(parents=True)
    _jsonl(tmp_path, f".trellis/tasks/{TASK}/implement.jsonl", [
        {"file": f".trellis/tasks/{TASK}/gone.md", "reason": "deleted target"},
    ])
    _commit(tmp_path)

    result = _run(tmp_path, "--audit")
    assert result.returncode != 0, "a dangling citation must not pass"
    out = result.stdout.decode()
    assert "DANGLING (1)" in out
    assert f".trellis/tasks/{TASK}/gone.md" in out


def test_archive_move_makes_a_citation_dangle(tmp_path):
    """The measured regression shape: moving the directory breaks a citation to
    a file inside it. This is what #678's A fix prevents at archive time; this
    guard is the net that reports it if that ever stops working."""
    _git_init(tmp_path)
    task = tmp_path / ".trellis" / "tasks" / TASK
    task.mkdir(parents=True)
    (task / "prd.md").write_bytes(b"# t\n")
    _jsonl(tmp_path, f".trellis/tasks/{TASK}/implement.jsonl", [
        {"file": f".trellis/tasks/{TASK}/prd.md", "reason": "own"},
    ])
    _commit(tmp_path)
    assert _run(tmp_path).returncode == 0

    # emulate the move without repointing -- exactly the pre-#678 behaviour
    dest = tmp_path / ".trellis" / "tasks" / "archive" / "2026-01" / TASK
    dest.parent.mkdir(parents=True)
    task.rename(dest)
    _commit(tmp_path)

    result = _run(tmp_path, "--audit")
    assert result.returncode != 0
    assert f".trellis/tasks/{TASK}/prd.md" in result.stdout.decode()


def test_basename_under_the_wrong_directory_is_dangling(tmp_path):
    """A slashed path is machine-typed, so it is held to an exact-path standard:
    naming the right basename under the wrong directory is a broken citation,
    not a near-miss (the shape of the two `type-safety.md` residuals in #680)."""
    _git_init(tmp_path)
    (tmp_path / ".trellis" / "spec" / "frontend").mkdir(parents=True)
    (tmp_path / ".trellis" / "spec" / "frontend" / "type-safety.md").write_bytes(b"x\n")
    task = tmp_path / ".trellis" / "tasks" / TASK
    task.mkdir(parents=True)
    _jsonl(tmp_path, f".trellis/tasks/{TASK}/implement.jsonl", [
        {"file": ".trellis/spec/backend/type-safety.md", "reason": "wrong dir"},
    ])
    _commit(tmp_path)

    assert _run(tmp_path).returncode != 0


def test_untracked_target_does_not_resolve(tmp_path):
    """Existence is judged on the TRACKED tree: a file present on disk but not
    committed is not part of the repository, so citing it is dangling. This is
    the T26 lesson -- `os.walk` would have called it present, giving two verdicts
    for one commit depending on whose machine ran the check.

    Built carefully: the citation must be COMMITTED (otherwise the corpus file
    is itself untracked and the guard legitimately ignores it, and the test
    would pass for the wrong reason).
    """
    _git_init(tmp_path)
    task = tmp_path / ".trellis" / "tasks" / TASK
    task.mkdir(parents=True)
    _jsonl(tmp_path, f".trellis/tasks/{TASK}/implement.jsonl", [
        {"file": f".trellis/tasks/{TASK}/ghost.md", "reason": "not committed"},
    ])
    _commit(tmp_path, "citation only")
    # `ghost.md` is now created on disk, and deliberately never committed
    (task / "ghost.md").write_bytes(b"on disk only\n")

    result = _run(tmp_path, "--audit")
    assert result.returncode != 0, "untracked file must not count as resolving"
    assert f".trellis/tasks/{TASK}/ghost.md" in result.stdout.decode()


# --------------------------------------------------------------------------
# MALFORMED -- a bad line must be reported, never swallowed
# --------------------------------------------------------------------------


def test_malformed_line_fails_and_is_reported(tmp_path):
    _git_init(tmp_path)
    task = tmp_path / ".trellis" / "tasks" / TASK
    task.mkdir(parents=True)
    _jsonl(tmp_path, f".trellis/tasks/{TASK}/implement.jsonl", [
        {"file": ".trellis/tasks/01-01-demo/prd.md"},
        "{not json",
    ])
    (task / "prd.md").write_bytes(b"# t\n")
    _commit(tmp_path)

    result = _run(tmp_path)
    assert result.returncode != 0
    out = result.stdout.decode()
    assert "MALFORMED (1)" in out
    assert "not valid JSON" in out


def test_json_array_inside_a_jsonl_file_is_malformed(tmp_path):
    """The real 2026-06/09 shape: a JSON array (one object per line, trailing
    commas) written into a `.jsonl` name. Valid as a file, invalid as JSONL --
    so every element line is reported rather than silently skipped, which is
    what would otherwise hide a citation."""
    _git_init(tmp_path)
    task = tmp_path / ".trellis" / "tasks" / TASK
    task.mkdir(parents=True)
    (task / "check.jsonl").write_bytes(
        b'[\n'
        b'  {"file": ".trellis/tasks/01-01-demo/prd.md", "reason": "a"},\n'
        b'  {"file": ".trellis/tasks/01-01-demo/prd.md", "reason": "b"},\n'
        b']\n'
    )
    _commit(tmp_path)

    result = _run(tmp_path)
    assert result.returncode != 0
    assert b"MALFORMED (4)" in result.stdout, result.stdout


def test_non_string_file_value_is_malformed(tmp_path):
    """`{"file": 42}` has no path to check. A checker that skipped it would be
    blind to precisely the line that was written wrong."""
    _git_init(tmp_path)
    task = tmp_path / ".trellis" / "tasks" / TASK
    task.mkdir(parents=True)
    _jsonl(tmp_path, f".trellis/tasks/{TASK}/implement.jsonl", [{"file": 42}])
    _commit(tmp_path)

    result = _run(tmp_path)
    assert result.returncode != 0
    assert b"not a string" in result.stdout, result.stdout


# --------------------------------------------------------------------------
# output-shape contracts
# --------------------------------------------------------------------------


def test_json_mode_is_machine_readable_and_carries_the_same_verdict(tmp_path):
    _git_init(tmp_path)
    task = tmp_path / ".trellis" / "tasks" / TASK
    task.mkdir(parents=True)
    _jsonl(tmp_path, f".trellis/tasks/{TASK}/implement.jsonl", [
        {"file": f".trellis/tasks/{TASK}/gone.md"},
    ])
    _commit(tmp_path)

    result = _run(tmp_path, "--json")
    assert result.returncode != 0
    rows = json.loads(result.stdout.decode())
    assert [r["verdict"] for r in rows] == ["DANGLING"]
    assert rows[0]["file"] == f".trellis/tasks/{TASK}/implement.jsonl"
    assert rows[0]["line"] == 1


def test_summary_line_accounts_for_every_reference(tmp_path):
    """ok + dangling must equal the scanned count -- the number the ticket's
    acceptance criterion asks to be independently reproducible."""
    _git_init(tmp_path)
    task = tmp_path / ".trellis" / "tasks" / TASK
    task.mkdir(parents=True)
    (task / "prd.md").write_bytes(b"# t\n")
    _jsonl(tmp_path, f".trellis/tasks/{TASK}/implement.jsonl", [
        {"file": f".trellis/tasks/{TASK}/prd.md"},
        {"file": f".trellis/tasks/{TASK}/gone.md"},
        {"file": f".trellis/tasks/{TASK}/gone2.md"},
    ])
    _commit(tmp_path)

    out = _run(tmp_path).stdout.decode()
    assert "scanned 3 " in out
    assert "1 ok / 2 dangling / 0 malformed" in out


def test_exclude_set_is_applied_on_top_of_git(tmp_path):
    """Both halves of the index are load-bearing, and this pins the second one.

    `EXCLUDE_DIRS` removes tracked paths that are scratch state. The pair is
    asserted directly against the guard's own helper rather than through a
    citation, because a `.trellis` citation can never point into an excluded
    directory -- which is itself the point: the exclude set only matters for
    paths that git tracks under a scratch name, and the repository really did
    track twelve `.scratch/durable-runtime-migration/**` files before
    `.scratch/` was ignored.

    A citation to an excluded path would also be skipped by `REF_PREFIX`, so the
    two rules are independent; this test keeps the second one from silently
    becoming dead code.
    """
    _git_init(tmp_path)
    (tmp_path / ".scratch").mkdir()
    (tmp_path / ".scratch" / "note.md").write_bytes(b"scratch\n")
    # force-track it, as the history did
    subprocess.run(["git", "add", "-f", ".scratch/note.md"], cwd=str(tmp_path),
                   check=True, capture_output=True)
    _commit(tmp_path)

    guard = _load_guard()
    # The helper reads the module-level ROOT, so drive it the way main() does.
    saved = guard.ROOT
    try:
        guard.ROOT = tmp_path
        index = guard._repo_index()
    finally:
        guard.ROOT = saved

    tracked = subprocess.run(["git", "ls-files"], cwd=str(tmp_path),
                             check=True, capture_output=True).stdout.decode()
    assert ".scratch/note.md" in tracked, "precondition: the file is tracked"
    assert ".scratch/note.md" not in index, "the exclude set is not being applied"
    assert "tracked.txt" in index, "the exclude set removed too much"


# --------------------------------------------------------------------------
# the real corpus -- read-only, and must reproduce the recorded numbers
# --------------------------------------------------------------------------


def test_real_corpus_reproduces_the_recorded_numbers():
    """Real corpus as of T40 / #690: 795 references / 0 dangling / 0 malformed
    (T40's own jsonl then moves it to 796 -- see the T40 note below).

    History: #680 recorded 787-or-788 references with 14 dangling / 47 malformed.
    The reference count was 787 and NOT 788-while-that-ticket-existed: the corpus
    is the tracked set, so #680's own `implement.jsonl` -- which cites
    `.trellis/workflow.md` under a `file` key -- only counted once committed.

    T31 cleaned the residue, and the numbers moved by exactly the amount the
    cleanup predicts:
      * 788 -> 785 references  = -3, the three deletions whose targets
        (`.trellis/spec/backend.md`, `.trellis/spec/backend/workspace-config-spec.md`
        x2) never existed anywhere in the repo -- they were dropped rather than
        invented.
      * 14 -> 0 dangling        = 11 repointed + 3 deleted.
      * 47 -> 0 malformed       = 27 trailing-comma lines + 18 array entries
        (the 2 files were `[ {..}, .. ]` arrays, whose 2 bracket lines also
        failed to parse).

    T32 / #682 added one `.trellis` citation of its own -- its `implement.jsonl`
    cites the ticket's `prd.md` -- so the count moved 787 -> 788 and nothing
    else did (still 0 dangling / 0 malformed). Same rule as above: the move is
    recorded here in the same change, not absorbed by a wider tolerance.

    T33 / #683 is the same move once more: its `implement.jsonl` cites its own
    `prd.md` and nothing else under `.trellis/`, so 788 -> 789. Its `check.jsonl`
    cites two in-repo paths (not `.trellis`-prefixed) and therefore adds nothing.

    T34 / #684 is that same move again: its `implement.jsonl` cites its own
    `prd.md` and nothing else under `.trellis/`, so 789 -> 790, and its
    `check.jsonl` cites two non-`.trellis` paths. The ticket's subject is lint
    coverage, which does not touch this corpus at all -- the +1 is purely the
    ticket's own existence, exactly as #682 and #683 were.

    T35 / #685 repeats the pattern a fourth time: its `implement.jsonl` cites its own
    `prd.md` and nothing else under `.trellis/`, so 790 -> 791, and its `check.jsonl`
    cites two non-`.trellis` paths. The opening line of this docstring read `as of T33 /
    #683: 788` until T35 -- stale for two tickets, and directly contradicted by the T34
    paragraph above it. It was fixed in the same change, per the rule that a statement
    the code has outgrown has to move with the code.

    T36 / #686 makes the same move a fifth time: its `implement.jsonl` cites its own
    `prd.md` and nothing else under `.trellis/`, so 791 -> 792, and its `check.jsonl`
    cites two non-`.trellis` paths. As with T34, the ticket's subject -- which
    workflow `paths` lists the Rust inputs the Python test job compiles -- does not
    touch this corpus at all; the +1 is purely the ticket's own existence.

    T37 / #687 makes the same move a sixth time: its `implement.jsonl` cites its own
    `prd.md` and nothing else under `.trellis/`, so 792 -> 793, and its `check.jsonl`
    cites two non-`.trellis` paths. The ticket's subject -- reconciling the README CI
    trigger table against `on.push.paths` in the workflow YAML -- does not touch this
    corpus at all; the +1 is purely the ticket's own existence.

    T38 / #688 makes the same move a seventh time: its `implement.jsonl` cites its own
    `prd.md` and nothing else under `.trellis/`, so 793 -> 794, and its `check.jsonl`
    cites two non-`.trellis` paths. The ticket's subject -- guarding the remaining
    hand-copied claims in the README CI prose -- does not touch this corpus at all;
    the +1 is purely the ticket's own existence.

    T39 / #689 makes the same move an eighth time: its `implement.jsonl` cites its own
    `prd.md` and nothing else under `.trellis/`, so 794 -> 795, and its `check.jsonl`
    cites two non-`.trellis` paths. The ticket's subject -- guarding the rule that a
    workflow's `run:` steps must be covered by that workflow's `paths` -- does not
    touch this corpus at all; the +1 is purely the ticket's own existence.

    T40 / #690 makes the same move a ninth time: its `implement.jsonl` cites its own
    `prd.md` and nothing else under `.trellis/`, so 795 -> 796, and its `check.jsonl`
    cites three non-`.trellis` paths. The ticket's subject -- normalising five files
    whose working-tree copies mixed CRLF with lone LF, and turning the journal-only
    mixed-ending ADVISORY into a repo-wide judgment -- does not touch this corpus at
    all; the +1 is purely the ticket's own existence.

    This is a tripwire, not a whitelist: if a future change moves any of these
    numbers, it must be updated in the same change.
    """
    result = _run(REPO_ROOT, "--json")
    rows = json.loads(result.stdout.decode())
    refs = [r for r in rows if r["verdict"] in {"OK", "DANGLING"}]
    dangling = [r for r in rows if r["verdict"] == "DANGLING"]
    malformed = [r for r in rows if r["verdict"] == "MALFORMED"]

    # The corpus is the TRACKED set, so this ticket's own `implement.jsonl`
    # counts only once committed: 795 while it is untracked, 796 once it is
    # (which is how CI always sees it). Those two ARE the reachable states for
    # the current ticket, so they are the pair. T31's window was wider only
    # because that ticket had two contributing jsonl files, giving it one
    # intermediate state; those values are unreachable now, and leaving them in
    # would mask a real -1 drift. Pinning the reachable set is the point.
    assert len(refs) in (795, 796), f"reference count drifted: {len(refs)}"
    assert len(dangling) == 0, f"dangling count drifted: {len(dangling)}"
    assert len(malformed) == 0, f"malformed count drifted: {len(malformed)}"


def test_untracked_carrier_is_not_audited(tmp_path):
    """The corpus is the TRACKED set, and this pins it.

    An untracked `.jsonl` carrier would otherwise be audited here and absent in
    CI -- two verdicts for one commit, the T26 defect relocated from the index
    to the corpus. The first version of this guard read the filesystem
    (`rglob`) and had exactly that hole.
    """
    _git_init(tmp_path)
    _commit(tmp_path, "baseline")
    task = tmp_path / ".trellis" / "tasks" / TASK
    task.mkdir(parents=True)
    # a draft carrier that is never committed, citing a path that does not exist
    _jsonl(tmp_path, f".trellis/tasks/{TASK}/implement.jsonl", [
        {"file": ".trellis/tasks/nowhere.md", "reason": "draft"},
    ])

    result = _run(tmp_path)
    assert result.returncode == 0, (
        "an untracked carrier was audited -- the corpus is not the tracked set"
    )
    assert b"scanned 0 " in result.stdout, result.stdout
