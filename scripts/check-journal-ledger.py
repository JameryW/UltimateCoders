"""Closeout check for the workspace journal ledger (#676).

Background.  `.trellis/scripts/add_session.py` appends a session skeleton that
contains placeholder text, and filling those placeholders is left to the
author's conscience.  Nothing verified it, so the ledger silently accumulated
**39 placeholder lines across 18 sections** (measured 2026-09-17, #674).  #674
cleaned the backlog; this tool exists so the backlog cannot come back.

STRUCTURAL (deterministic, fail-closed -- exit 1):
  PLACEHOLDER     a line whose *stripped* content EQUALS a skeleton placeholder
  HEADING_COUNT   a session does not carry each of the six standard headings
                  exactly once (missing -> the S16/17/18 shape; duplicated ->
                  the leftover-skeleton-tail shape)
  SESSION_NUMBER  `## Session N` with no number, or one number used twice
  EMPTY_CORPUS    no journal file, or a journal with no `## Session` heading --
                  without this the check would pass vacuously on a broken
                  checkout, which is the one failure a gate must not have
  NO_INDEX        `git ls-files` could not be asked what the ledger contains
  STALE_SKELETON  `add_session.py` no longer contains a declared heading or a
                  declared placeholder.  This is the check watching *itself*:
                  if the skeleton is renamed, the declared strings stop
                  describing reality and the gate would silently test nothing.
  LEGACY_DRIFT    a journal in LEGACY_JOURNALS no longer matches its pinned
                  numbers (see below)

ADVISORY (heuristic, never fails the build):
  line endings are not uniform; `index.md`'s `Total Sessions` disagrees with
  the measured count.

WHY THE CRITERION IS "WHOLE LINE EQUALS" AND NOT A SUBSTRING.
A substring criterion is *self-referentially red*: the sessions that discuss
these very placeholders quote them in prose.  Measured on a CLEAN ledger
(journal-2.md, 2026-09-17) -- loose `marker in line` / strict
`line.strip() == marker`:

    '- [OK] (Add test results)'   1 / 0
    '- None - task complete'      3 / 0
    '(Add details)'               4 / 0

So a substring gate would be red on a clean repo, get switched off, and that is
worse than no gate at all.  The cost of the strict form is a documentation rule
the ledger must respect: when a session *quotes* a placeholder, give the line a
prefix (a list marker, backticks, table pipes) so it is not the placeholder
alone.  The violation message says so.

WHY THE INDEX COMES FROM GIT.
`iter_journal_paths` asks `git ls-files` instead of walking the filesystem.  A
walk sees files a clean checkout does not have -- #675 slice C measured a guard
whose verdict depended on a gitignored `.codex/config.toml`, so the same commit
produced two different judgments.  It also would see a locally-created
developer directory, which no CI runner has.  The same reasoning makes the
`--root` scope explicit: `.trellis/.developer` (which names the active
developer) is **gitignored**, so "the active ledger" is not a value CI can
compute.  Hence the corpus is "every journal git tracks", plus LEGACY_JOURNALS.

WHY LEGACY_JOURNALS EXISTS.
`.trellis/workspace/JameryW/` is a second, tracked ledger (last active
2026-08-06, before the T-series) holding 363 placeholder lines across 123
sessions.  #676 scoped the fix to the ledger the delivery flow writes, so those
files are not cleaned here -- but they are not silently ignored either: their
numbers are **pinned**, and a pin that stops matching fails the check.  So the
debt can neither grow nor be forgotten, and a new journal file (which is ours)
must be perfect.  Counted debt is visible; uncounted is not.

Deliberately NOT gated, even though the skeleton emits them:
  `(No commits - planning session)` and `[OK] **Completed**` are legitimate
  content -- a planning session genuinely has no commits, and `Completed` is a
  real status.  Gating them would produce the same permanently-red shape.
  (The asymmetry is on purpose: same emitter, different meaning.)

`--verbose` only adds output; it never changes the verdict.  (The sibling guard
`check-spec-refs.py` uses `--audit` for an advisory-only mode, so the flags are
named differently on purpose rather than made to look alike.)

Exit code 0 means "the ledger conforms"; 1 lists every violation.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKSPACE_DIR = ROOT / ".trellis" / "workspace"
SKELETON = ROOT / ".trellis" / "scripts" / "add_session.py"

# The six headings `generate_session_content` emits.  Kept as a declared list
# rather than derived, so that *adding* a heading to the skeleton stays a
# deliberate decision -- but every item is checked against the skeleton below,
# so a rename cannot leave this list describing a skeleton that no longer
# exists.
DECLARED_HEADINGS = (
    "### Summary",
    "### Main Changes",
    "### Git Commits",
    "### Testing",
    "### Status",
    "### Next Steps",
)

# Default values `add_session.py` is prepared to write.  `(Add summary)` is the
# `--summary` default; the other three are the template's own text.
DECLARED_PLACEHOLDERS = (
    "- [OK] (Add test results)",
    "- None - task complete",
    "(Add details)",
    "(Add summary)",
)

# Emitted by the skeleton but legitimate; see the module docstring.
SKELETON_LEGITIMATE = (
    "(No commits - planning session)",
    "[OK] **Completed**",
)

# Keys are relative to WORKSPACE_DIR.  Measured 2026-09-17 (see the docstring):
# pinned so the other ledger's debt is frozen and visible rather than ignored.
LEGACY_JOURNALS = {
    "JameryW/journal-1.md": {"placeholders": 165, "sessions": 56, "headings_ok": 56},
    "JameryW/journal-2.md": {"placeholders": 168, "sessions": 57, "headings_ok": 56},
    "JameryW/journal-3.md": {"placeholders": 30, "sessions": 10, "headings_ok": 10},
}

SESSION_RE = re.compile(r"^##\s*Session\b")
SESSION_NUM_RE = re.compile(r"^##\s*Session\s+(\d+)\b")


class Violation:
    """One reason the ledger does not conform."""

    __slots__ = ("path", "line", "kind", "detail")

    def __init__(self, path: str, line: int, kind: str, detail: str) -> None:
        self.path = path
        self.line = line
        self.kind = kind
        self.detail = detail

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.kind}: {self.detail}"


def rel(path: pathlib.Path) -> str:
    """Path relative to the repo root when possible, for stable messages."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def iter_journal_paths(
    workspace_dir: pathlib.Path | None = None,
) -> tuple[list[pathlib.Path], str | None]:
    """Journals as GIT knows them.

    Returns `(paths, error)`.  Never `os.walk`: see the module docstring.  The
    pathspec is derived from `workspace_dir` so a synthetic root works, but the
    command still runs against the repository that contains it.
    """
    base = WORKSPACE_DIR if workspace_dir is None else workspace_dir
    try:
        spec = base.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        spec = base.resolve().as_posix()
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--", spec],
        cwd=str(ROOT), capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or "no stderr"
        return [], f"`git ls-files -- {spec}` failed (rc={proc.returncode}): {detail}"
    names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]
    paths = []
    for name in names:
        path = ROOT / name
        if path.name.startswith("journal-") and path.name.endswith(".md") and path.is_file():
            paths.append(path)
    return sorted(paths), None


def read_journal(path: pathlib.Path) -> tuple[list[str], int, int]:
    """Decode a journal, normalising CRLF.

    Returns the lines and the raw `\\r\\n` / lone-`\\n` counts.  `strip()` below is
    what makes the matching robust on a CRLF checkout: a criterion that only
    stripped spaces would compare `marker + "\\r"` and silently never match
    anything (#676 is the third time this trap has appeared).
    """
    raw = path.read_bytes()
    crlf = raw.count(b"\r\n")
    lone_lf = raw.count(b"\n") - crlf
    return raw.decode("utf-8").replace("\r\n", "\n").split("\n"), crlf, lone_lf


def journal_key(path: pathlib.Path) -> str:
    """Path relative to WORKSPACE_DIR -- the key LEGACY_JOURNALS is written in."""
    try:
        return path.resolve().relative_to(WORKSPACE_DIR.resolve()).as_posix()
    except ValueError:
        return path.name


def sessions_of(lines: list[str]) -> list[tuple[int | None, int, int]]:
    """Split into sessions as `(number_or_None, start_index, end_index)`.

    A session start is any line beginning `## Session`, not only a *well formed*
    one: detection has to be wider than parsing, or a heading that lost its
    number (`## Session: ...`) is not a broken session -- it is no session at
    all, and the whole section including its placeholders goes unread.  Sliced on
    collected heading positions rather than `re.split`: a duplicated heading makes
    the split misalign, and a session with no `### Testing` makes a `str.index`
    lookup raise (#674 measured both).
    """
    starts = [i for i, x in enumerate(lines) if SESSION_RE.match(x.strip())]
    bounds = starts + [len(lines)]
    out: list[tuple[int | None, int, int]] = []
    for k, s in enumerate(starts):
        m = SESSION_NUM_RE.match(lines[s].strip())
        out.append((int(m.group(1)) if m else None, s, bounds[k + 1]))
    return out


def placeholder_lines(
    lines: list[str], markers: tuple[str, ...] | None = None,
) -> list[tuple[int, str]]:
    """Lines that ARE a placeholder -- whole-line equality, never a substring."""
    wanted = DECLARED_PLACEHOLDERS if markers is None else markers
    return [(i + 1, x.strip()) for i, x in enumerate(lines) if x.strip() in wanted]


def skeleton_source(skeleton: pathlib.Path | None = None) -> str | None:
    path = SKELETON if skeleton is None else skeleton
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def check_skeleton(skeleton: pathlib.Path | None = None) -> list[Violation]:
    """Fail closed when the skeleton stops matching what this tool declares."""
    path = SKELETON if skeleton is None else skeleton
    name = rel(path)
    source = skeleton_source(path)
    if source is None:
        return [Violation(name, 0, "STALE_SKELETON",
                          "the session skeleton is missing, so this check cannot "
                          "know what a placeholder looks like")]
    emitted = {x.rstrip() for x in source.split("\n") if x.startswith("### ")}
    problems = []
    for heading in DECLARED_HEADINGS:
        if heading not in emitted:
            problems.append(Violation(
                name, 0, "STALE_SKELETON",
                f"{heading!r} is declared standard but the skeleton no longer emits it "
                f"(skeleton emits: {sorted(emitted)})"))
    for marker in DECLARED_PLACEHOLDERS:
        if marker not in source:
            problems.append(Violation(
                name, 0, "STALE_SKELETON",
                f"{marker!r} is declared a placeholder but no longer appears in the "
                f"skeleton -- update DECLARED_PLACEHOLDERS"))
    return problems


def measure_journal(path: pathlib.Path) -> tuple[list[Violation], dict]:
    """Measure one journal and, if it is ours, hold it to the structural rules.

    Legacy journals are measured only: their pinned numbers must still match.
    """
    name = rel(path)
    key = journal_key(path)
    lines, crlf, lone_lf = read_journal(path)
    problems: list[Violation] = []
    sections = sessions_of(lines)

    headings_ok = 0
    numbers: list[int] = []
    heading_problems: list[Violation] = []
    number_problems: list[Violation] = []
    for number, start, end in sections:
        seg = lines[start:end]
        if number is None:
            number_problems.append(Violation(
                name, start + 1, "SESSION_NUMBER",
                f"a '## Session' heading that is not the skeleton's '## Session N: title' "
                f"form ({lines[start].strip()[:60]!r}) -- either a typo, or the whole section "
                f"is invisible to this check"))
        else:
            numbers.append(number)
        off = []
        for heading in DECLARED_HEADINGS:
            found = [i for i, x in enumerate(seg) if x.strip() == heading]
            if len(found) != 1:
                where = "missing" if not found else "x{} (lines {})".format(
                    len(found), ", ".join(str(start + i + 1) for i in found))
                off.append(f"{heading} {where}")
        if off:
            heading_problems.append(Violation(
                name, start + 1, "HEADING_COUNT",
                f"{lines[start].strip()[:50]!r}: " + "; ".join(off)))
        else:
            headings_ok += 1

    ph_lines = placeholder_lines(lines)
    stats = {
        "path": name,
        "key": key,
        "lines": len(lines),
        "sessions": len(sections),
        "headings_ok": headings_ok,
        "numbers": numbers,
        "crlf": crlf,
        "lone_lf": lone_lf,
        "placeholders": len(ph_lines),
        "legacy": key in LEGACY_JOURNALS,
    }

    if stats["legacy"]:
        pin = LEGACY_JOURNALS[key]
        for field in ("placeholders", "sessions", "headings_ok"):
            if pin[field] != stats[field]:
                problems.append(Violation(
                    name, 0, "LEGACY_DRIFT",
                    f"{field} is {stats[field]} but {key!r} is pinned at {pin[field]} in "
                    f"LEGACY_JOURNALS -- either the other ledger changed (decide whether to "
                    f"clean it up) or the pin is stale (re-pin deliberately)"))
        return problems, stats

    for line_no, marker in ph_lines:
        problems.append(Violation(
            name, line_no, "PLACEHOLDER",
            f"unfilled skeleton placeholder {marker!r}; if this line is *discussing* "
            f"the placeholder rather than owing it, give it a prefix (a list marker "
            f"or backticks) -- the criterion is whole-line equality on purpose"))
    if not sections:
        problems.append(Violation(
            name, 0, "EMPTY_CORPUS",
            "no '## Session' heading -- a journal the check cannot read is not a "
            "journal that passed"))
    problems.extend(heading_problems)
    problems.extend(number_problems)
    return problems, stats


def index_total(index_path: pathlib.Path) -> int | None:
    """`Total Sessions` from an index.md, or None when absent."""
    if not index_path.is_file():
        return None
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if "Total Sessions" in line:
            m = re.search(r":\s*(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the workspace journal ledger for skeleton placeholders "
                    "and session-structure drift (#676).")
    parser.add_argument("--verbose", action="store_true",
                        help="print the per-journal census (verdict unchanged)")
    args = parser.parse_args(argv)

    problems = check_skeleton()
    journals, index_error = iter_journal_paths()
    if index_error:
        problems.append(Violation(rel(WORKSPACE_DIR), 0, "NO_INDEX", index_error))
    if not journals:
        problems.append(Violation(
            rel(WORKSPACE_DIR), 0, "EMPTY_CORPUS",
            "git tracks no journal-*.md under <workspace>/<developer>/ -- refusing to "
            "report success on an empty corpus"))

    stats_all = []
    seen: dict[int, str] = {}
    for path in journals:
        found, stats = measure_journal(path)
        problems.extend(found)
        stats_all.append(stats)

    ours = [s for s in stats_all if not s["legacy"]]
    for s in ours:
        for number in s["numbers"]:
            if number in seen:
                problems.append(Violation(
                    s["path"], 0, "SESSION_NUMBER",
                    f"session {number} is used twice (also in {seen[number]})"))
            else:
                seen[number] = s["path"]

    total_sessions = sum(s["sessions"] for s in stats_all)
    total_lines = sum(s["lines"] for s in stats_all)
    total_ph = sum(s["placeholders"] for s in stats_all)
    legacy = [s for s in stats_all if s["legacy"]]
    legacy_ph = sum(s["placeholders"] for s in legacy)
    ok_sessions = sum(s["headings_ok"] for s in ours)

    print(f"journal ledger: {len(journals)} journal file(s), "
          f"{total_sessions} session(s), {total_lines} line(s), "
          f"{total_ph} placeholder line(s)")
    for s in stats_all:
        if s["legacy"]:
            # Pinned, not judged: `!!` would read as a failure while the exit
            # code says the corpus conforms.
            mark = "pin "
        else:
            clean = s["headings_ok"] == s["sessions"] and not s["placeholders"]
            mark = "ok  " if clean else "!!  "
        printed = s["path"] if not s["legacy"] else s["path"] + " [legacy]"
        print(f"  {mark}{printed}: {s['sessions']} session(s), "
              f"{s['placeholders']} placeholder line(s), "
              f"{s['headings_ok']}/{s['sessions']} with all {len(DECLARED_HEADINGS)} headings")
    print(f"  this ledger: {len(ours)} file(s), {len(seen)} session(s), "
          f"{sum(s['placeholders'] for s in ours)} placeholder line(s), "
          f"{ok_sessions}/{sum(s['sessions'] for s in ours)} session(s) conforming")
    print(f"  legacy (pinned, not fixed here): {len(legacy)} file(s), "
          f"{sum(s['sessions'] for s in legacy)} session(s), {legacy_ph} placeholder line(s)")

    if args.verbose:
        for s in stats_all:
            print(f"  -- {s['path']}: numbers={sorted(s['numbers'])} "
                  f"crlf={s['crlf']} lone_lf={s['lone_lf']}")

    # --- advisories: printed, never a verdict -----------------------------
    for s in stats_all:
        if s["lone_lf"]:
            print(f"ADVISORY: {s['path']} mixes line endings "
                  f"({s['lone_lf']} lone LF vs {s['crlf']} CRLF)")
    for dev_dir in sorted({p.parent for p in journals}):
        total = index_total(dev_dir / "index.md")
        if total is None:
            continue
        have = sum(s["sessions"] for s in stats_all if s["path"].startswith(rel(dev_dir) + "/"))
        if have and total != have:
            print(f"ADVISORY: {rel(dev_dir)}/index.md says Total Sessions: {total} "
                  f"but the journals hold {have}")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in sorted(problems, key=lambda v: (v.kind, v.path, v.line)):
            print(f"  {p}")
        print("\njournal ledger check FAILED.")
        return 1
    print("journal ledger check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
