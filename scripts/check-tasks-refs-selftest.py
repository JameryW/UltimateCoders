#!/usr/bin/env python3
"""Mutation self-check for `scripts/check-tasks-refs.py`.

Why this file exists: a checker nobody has ever seen go red is not evidence.
This repository has paid for that lesson more than once, so the guard that
#680 adds ships with the procedure that falsifies it.

Procedure, in order:

  1. confirm the target suite is GREEN before touching anything
  2. apply ONE mutation at a time to the guard
  3. confirm it turns at least one test red
  4. restore byte-exactly, then have a SEPARATE process recompute sha256 --
     a restored file read back by the process that wrote it only proves the
     writer's own buffer, so that is not certification
  5. require that no two mutations redden an IDENTICAL set: identical sets mean
     one of the two is decoration, not coverage

Step 5 is the part that catches tests which pass for the wrong reason. It fired
for real here: the first pass had four mutations and the exclude-set test
reddened under none of them, i.e. it was unpinned. Adding
`empty-exclude-set` -- found by a targeted probe rather than by reading --
pinned it, and mutation 6 was added after review found the corpus still came
from the filesystem while the index came from git.

Usage:  python scripts/check-tasks-refs-selftest.py
Exit 0 = every mutation reddens and all sets are distinct.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check-tasks-refs.py"
TESTFILE = "tests/python/test_check_tasks_refs.py"

# (name, old, new).  Each entry must mutate exactly ONE behaviour, so that a
# red set can be attributed.  An anchor that matches anything other than once
# is a hard failure -- silent no-op mutations are how a self-check lies.
MUTATIONS: list[tuple[str, str, str]] = [
    # 1. existence is decided by the filesystem instead of the tracked tree, so
    #    an untracked file resolves. ONE line, one axis -- note `git ls-files
    #    --others` was tried first and is a sledgehammer: `--others` REPLACES
    #    the tracked list (verified: it prints [] in a clean repo), so the index
    #    empties and 13 of 14 tests redden. It still reddens, but a mutation
    #    that removes the whole feature cannot localise anything.
    ("existence-from-filesystem",
     '            is_ok = posix in index',
     '            is_ok = (ROOT / posix).exists()'),
    # 2. an unparsable line is swallowed instead of reported.
    ("swallow-malformed",
     '                rows.append({\n'
     '                    "file": rel, "line": lineno, "ref": None,\n'
     '                    "verdict": MALFORMED, "detail": "not valid JSON",\n'
     '                })\n'
     '                continue',
     '                continue'),
    # 3. nothing is ever dangling.
    ("never-dangling",
     '                "verdict": OK if is_ok else DANGLING,',
     '                "verdict": OK,'),
    # 4. the `.trellis` prefix filter is dropped: any `file` value counts.
    ("no-prefix-filter",
     '            if not ref.startswith(REF_PREFIX):\n'
     '                continue',
     '            if not isinstance(ref, str):\n'
     '                continue'),
    # 5. the exclude set is emptied, so a tracked scratch path is a repo path.
    ("empty-exclude-set",
     'EXCLUDE_DIRS = {".git", ".scratch", "target", "node_modules", "vendor", ".venv",\n'
     '                "__pycache__", "dist", "build", ".pytest_cache"}',
     'EXCLUDE_DIRS = set()'),
    # 6. the corpus goes back to the filesystem, so an untracked carrier is
    #    audited and one commit has two verdicts.
    ("corpus-from-filesystem",
     '    corpus = sorted(\n'
     '        rel for rel in index\n'
     '        if rel.startswith(prefix) and rel.endswith(".jsonl")\n'
     '    )',
     '    corpus = sorted(\n'
     '        p.relative_to(ROOT).as_posix()\n'
     '        for p in (ROOT / TASKS_DIR_NAME).rglob("*.jsonl") if p.is_file()\n'
     '    )'),
    # NOTE: a CRLF mutation was attempted twice and removed both times.
    #   * mutating a trailing-CR branch only made the guard crash (a crash
    #     reddens a test without pinning the axis)
    #   * `split("\n")` instead of `splitlines()` changed NOTHING, because
    #     `line.strip()` removes the CR regardless
    # So the guard is EOL-robust by construction and there is nothing to pin.
    # The CRLF test was removed with them: it asserted a property that could not
    # fail, which is decoration. The measurement is recorded in the ticket.
]


def _python() -> str:
    """The interpreter running this check -- no venv path, so it is portable."""
    return sys.executable


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _env() -> dict:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "selftest",
        "GIT_AUTHOR_EMAIL": "selftest@example.com",
        "GIT_COMMITTER_NAME": "selftest",
        "GIT_COMMITTER_EMAIL": "selftest@example.com",
    })
    return env


def run_tests() -> set[str]:
    """Names of failing tests, parsed from pytest's short summary.

    Raises if pytest did not actually run.  Without that check, a launch failure
    makes every red set empty -- and an empty set is indistinguishable from
    "nothing failed", so the self-check would report 21 identical-set problems
    pointing at the mutations instead of at the real cause. Observed once
    (2026-09-18) under heavy machine load.
    """
    proc = subprocess.run(
        [_python(), "-m", "pytest", TESTFILE, "-q", "-o", "addopts=",
         "-p", "no:cacheprovider", "--tb=no"],
        cwd=str(ROOT), capture_output=True, env=_env(),
    )
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    # pytest -q ends with a one-line summary that contains one of these words.
    # Absent all three, pytest did not reach its summary (launch failure, an
    # import error before collection, ...) and every red set would come back
    # empty -- which looks exactly like "every mutation is a no-op".
    if not any(word in out for word in ("passed", "failed", "error")):
        raise RuntimeError(f"pytest produced no summary (rc={proc.returncode}):\n"
                           f"--- stdout ---\n{out}\n--- stderr ---\n{err}")
    failed: set[str] = set()
    for line in out.splitlines():
        if line.startswith("FAILED "):
            failed.add(line.split("::")[-1].split(" ")[0])
    return failed


def certify(expect: str) -> bool:
    """Recompute the guard's hash in a SEPARATE process.

    Retried once: an empty read is a transient (the file is restored and then
    immediately re-spawned), and a transient must not be reported as a failed
    restore -- nor may it be silently accepted. Two attempts, then give up with
    the evidence printed.
    """
    argv = [_python(), "-c",
            "import hashlib,pathlib,sys;"
            "print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())",
            str(GUARD)]
    for attempt in (1, 2):
        proc = subprocess.run(argv, cwd=str(ROOT), capture_output=True, env=_env())
        got = proc.stdout.decode("utf-8", "replace").strip()
        if got == expect:
            return True
        print(f"  certify attempt {attempt}: got {got!r} rc={proc.returncode} "
              f"stderr={proc.stderr.decode('utf-8', 'replace')!r}")
    print(f"  !! restore NOT certified: expected {expect!r}")
    return False


def main() -> int:
    try:
        original = GUARD.read_bytes()
    except OSError as exc:
        print(f"ABORT: cannot read {GUARD}: {exc}")
        return 1
    base = sha256(GUARD)
    print(f"guard            = {GUARD.relative_to(ROOT)}")
    print(f"baseline sha256  = {base}")

    before = run_tests()
    print(f"baseline red set = {sorted(before) or 'none (green)'}")
    if before:
        print("ABORT: the suite is not green before mutation")
        return 1

    results: dict[str, set[str]] = {}
    for name, old, new in MUTATIONS:
        text = original.decode("utf-8")
        count = text.count(old)
        if count != 1:
            print(f"ABORT: mutation {name}: anchor matched {count} times "
                  f"(expected exactly 1)")
            return 1
        mutated = text.replace(old, new).encode("utf-8")
        if mutated == original:
            print(f"ABORT: mutation {name} changed nothing")
            return 1

        GUARD.write_bytes(mutated)
        try:
            red = run_tests()
        finally:
            GUARD.write_bytes(original)
        if not certify(base):
            return 1

        results[name] = red
        status = "RED" if red else "no effect"
        print(f"mutation {name:24s} {status:9s} "
              f"{len(red):2d} test(s): {sorted(red)}")

    if GUARD.read_bytes() != original:
        print("ABORT: restore is not byte-exact")
        return 1

    problems = 0
    print("\npairwise check (identical red sets = one is decoration):")
    names = sorted(results)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if results[a] == results[b]:
                print(f"  FAIL {a} and {b} redden the SAME set")
                problems += 1
    print("  no identical sets" if problems == 0 else "")
    for name in names:
        if not results[name]:
            print(f"  FAIL {name} reddened NOTHING (its anchor is a no-op)")
            problems += 1

    if problems:
        print(f"\nSELF-CHECK FAILED ({problems} problem(s))")
        return 1
    print(f"\nSELF-CHECK PASSED: {len(results)} mutations, all distinct, "
          f"restore certified at {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
