"""Audit `.trellis` references carried by task context files.

Background (issue #678, option C; ticket #680).  `.trellis/tasks/**/*.jsonl`
records a task's context as `{"file": ".trellis/...", "reason": "..."}`.  Those
references live outside every guard this repository has: `check-spec-refs.py`
indexes `SPEC_DIR = .trellis/spec` only, so nothing reported a task-context
citation that stopped resolving.

#678 fixed the *cause* -- `archive_task_dir` now repoints a task's own citations
in the same operation that moves the directory (`7ba7ad8`) and the accumulated
corpus was migrated (`f3ee6dd`).  It also wrote down its own recommendation as
**"A + C"**, where C is "make 'a `.trellis/tasks/**/*.jsonl` `.trellis`
reference must resolve' a checker".  A shipped; **C was never built** -- hence
this file.  It guards against *recurrence*, it does not migrate anything: the
residual 14 citations are recorded in #680 and are deliberately out of scope
(two of them are new shapes whose correct fix is not mechanical -- a citation
whose target moved to the retired `.trellis/workspace/JameryW/`, and one where a
task directory was archived *inside itself*).

STRUCTURAL (deterministic, fail-closed -- exit 1):

  DANGLING       the referenced path does not exist in the tracked tree.
                 That includes a citation that names the right basename under
                 the wrong directory: a slashed path is a machine-typed path,
                 and holding it to an exact-path standard is the point.

MALFORMED (also structural):
  a line that is not valid JSON, or whose `file` value is not a string.  A
  silently-skipped unparsable line is exactly where this class of defect hides,
  so it is reported rather than swallowed.

ADVISORY (never fails the build):
  none yet -- kept as an explicit note so the shape of this tool is legible next
  to `check-spec-refs.py`, which has an `--audit` table for its heuristics.

Index source: `git ls-files` **intersected with** `EXCLUDE_DIRS`.  The CORPUS
(which `.jsonl` files get read) is the tracked set too, for the same reason --
see `collect`.

Both halves are load-bearing, and both are inherited from measurements rather
than taste:

  * `os.walk` sees files that are ignored by git, so the same commit produced
    two different verdicts on a developer's box vs. a clean checkout (T26,
    2026-09-17: a gitignored `.codex/config.toml` flipped a verdict).  The index
    therefore comes from git.
  * A tracked path under `.scratch/` is still in the repository, so the exclude
    set is applied *on top of* git rather than replaced by it.  That case is
    real here: twelve tracked `.scratch/durable-runtime-migration/**` files were
    committed before `.scratch/` was ignored.

CI: this guard reads an OPEN input set -- any task directory that gets archived
can flip its verdict, and so can any file that a task cites -- so it is wired
without a `paths` filter.  A filter would blind it to exactly the changes that
matter.  That is the opposite of `ci-trellis.yml`, whose input set is closed;
`check-spec-refs.py` is the same shape as this file and is wired the same way.

`--root DIR` points the guard at another tree.  Without it the tree is the one
this file lives in, which makes the guard untestable: a test that builds a
synthetic repository in a temporary directory would silently re-audit the real
one and pass on a coincidence.  That is not hypothetical -- the first version
of the test suite did exactly that, and the failure was visible only because
the synthetic expectations disagreed with the real corpus.  The option is for
tests; CI runs it bare so the verdict is always about the checked-out commit.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from typing import Any

DEFAULT_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Set from `--root` in main(); see the docstring.  A module-level value rather
# than a threaded parameter because every function here needs it and the guard
# is a one-shot script.
ROOT = DEFAULT_ROOT

TASKS_DIR_NAME = pathlib.Path(".trellis") / "tasks"

# Applied to the `git ls-files` output.  See the module docstring: git is
# authoritative for *existence*, and this set only removes tracked paths that
# are scratch state.
EXCLUDE_DIRS = {".git", ".scratch", "target", "node_modules", "vendor", ".venv",
                "__pycache__", "dist", "build", ".pytest_cache"}

# The prefix that makes a `file` value a repository reference at all.  A value
# that does not start with this is not this guard's business (it may be a bare
# filename or external material).
REF_PREFIX = ".trellis"

DANGLING = "DANGLING"
MALFORMED = "MALFORMED"
OK = "OK"


def _git_ls_files() -> list[str]:
    """Tracked paths, `git ls-files`, one per line, converted to posix form.

    `git ls-files` and not `os.walk`: see the module docstring.  Bytes are read
    from the subprocess (not `text=True`) because on Windows `text=True`
    translates `\\n` to `\\r\\n` on the way *in*, which would leave a stray CR on
    every path and make set comparisons silently mismatch while still exiting 0
    (a trap this repository has already paid for once).
    """
    proc = subprocess.run(["git", "ls-files"],
                          capture_output=True, cwd=ROOT)
    if proc.returncode != 0:
        print("error: `git ls-files` failed -- this guard needs a git checkout",
              file=sys.stderr)
        print(proc.stderr.decode("utf-8", "replace"), file=sys.stderr)
        raise SystemExit(2)
    out = proc.stdout.decode("utf-8", "replace")
    return [line for line in out.splitlines() if line]


def _repo_index() -> set[str]:
    """Tracked files that survive `EXCLUDE_DIRS`, as posix relative paths."""
    index: set[str] = set()
    for raw in _git_ls_files():
        posix = raw.replace("\\", "/")
        parts = posix.split("/")
        if any(part in EXCLUDE_DIRS for part in parts):
            continue
        index.add(posix)
    return index


def collect(index: set[str]) -> list[dict[str, Any]]:
    """One row per `.trellis`-prefixed `file` value found in task context files.

    The corpus is the TRACKED set, not what happens to be on disk. That matters:
    a `.jsonl` carrier that is untracked (a draft, a scratch copy) would
    otherwise be audited on the author's machine and absent in CI, which is two
    verdicts for one commit -- the same defect the index avoids by coming from
    git, just moved from the index to the corpus. The first version of this
    guard read the filesystem here and had exactly that hole.
    """
    rows: list[dict[str, Any]] = []
    prefix = TASKS_DIR_NAME.as_posix() + "/"
    corpus = sorted(
        rel for rel in index
        if rel.startswith(prefix) and rel.endswith(".jsonl")
    )
    for rel in corpus:
        path = ROOT / rel
        if not path.is_file():
            # Tracked but absent: a dirty working tree (`git rm` without commit).
            # Not this guard's business; the path is what the commit says.
            continue
        text = path.read_bytes().decode("utf-8", "replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                rows.append({
                    "file": rel, "line": lineno, "ref": None,
                    "verdict": MALFORMED, "detail": "not valid JSON",
                })
                continue
            # The carriers are objects; anything else has no `file` to check and
            # is not this guard's concern (a bare list is not a citation).
            if not isinstance(payload, dict) or "file" not in payload:
                continue
            ref = payload["file"]
            if not isinstance(ref, str):
                rows.append({
                    "file": rel, "line": lineno, "ref": ref,
                    "verdict": MALFORMED,
                    "detail": f"`file` is {type(ref).__name__}, not a string",
                })
                continue
            if not ref.startswith(REF_PREFIX):
                continue
            posix = ref.replace("\\", "/")
            is_ok = posix in index
            rows.append({
                "file": rel, "line": lineno, "ref": ref,
                "verdict": OK if is_ok else DANGLING,
                "detail": "" if is_ok else "no tracked file matches this path",
            })
    return rows


def main() -> int:
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audit", action="store_true",
                        help="list every dangling citation, with its carrier and line")
    parser.add_argument("--json", action="store_true", help="emit raw rows as JSON")
    parser.add_argument("--root", default=None, metavar="DIR",
                        help="audit a different tree (for tests; CI omits it)")
    args = parser.parse_args()

    if args.root is not None:
        ROOT = pathlib.Path(args.root).resolve()
        if not (ROOT / ".git").exists():
            print(f"error: {ROOT} is not a git checkout", file=sys.stderr)
            return 2

    index = _repo_index()
    rows = collect(index)
    refs = [r for r in rows if r["verdict"] in {OK, DANGLING}]
    dangling = [r for r in rows if r["verdict"] == DANGLING]
    malformed = [r for r in rows if r["verdict"] == MALFORMED]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 1 if (dangling or malformed) else 0

    print(f"scanned {len(refs)} `.trellis` reference(s) in task context files")
    print(f"indexed {len(index)} tracked path(s) (`git ls-files` minus "
          f"{len(EXCLUDE_DIRS)} excluded dir name(s))")
    if malformed:
        print(f"\nMALFORMED ({len(malformed)}):")
        for r in malformed:
            print(f"  {r['file']}:{r['line']}  {r['detail']}")
    if dangling and args.audit:
        print(f"\nDANGLING ({len(dangling)}):")
        for r in dangling:
            print(f"  {r['file']}:{r['line']}  ->  {r['ref']}")

    problems = len(dangling) + len(malformed)
    print(f"\nsummary: {len(refs) - len(dangling)} ok / {len(dangling)} dangling / "
          f"{len(malformed)} malformed")
    if problems:
        print("task context reference audit FAILED.")
        if not args.audit:
            print("(re-run with --audit to list them)")
        return 1
    print("task context reference audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
