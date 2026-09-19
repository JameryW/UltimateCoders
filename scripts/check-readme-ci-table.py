#!/usr/bin/env python3
"""Reconcile the README CI trigger table against the workflow YAML.

Two READMEs carry a table that says, per workflow, which paths trigger it. That
table is a hand-written copy of `on.push.paths` in `.github/workflows/*.yml`,
and nothing checked it. T36 / #686 measured what that costs: rewriting the table
by hand produced `` `docs/agents/*.md` `` for `ci-codex-flow.yml`, a glob that
does not exist in that file -- it has four named files. It looked entirely
plausible and no amount of re-reading would have caught it; a reconciliation
script caught it immediately.

This guard is that script, kept. It compares, for each workflow:

  1. non-vacuity   -- the parsers actually saw rows and workflows
  2. existence     -- table's workflow set == the set on disk
  3. symmetry      -- push.paths == pull_request.paths (one cell cannot describe both)
  4. no fabrication-- claimed paths are all real
  5. no omission   -- real paths are all claimed
  6. filter shape  -- an unfiltered workflow claims nothing, and vice versa
  7. branches      -- every workflow targets `main`, as the prose states
  8. prose count   -- the count word in the CI prose equals the number of
                      workflow files, and a count word must be present at all
                      (otherwise a rewording retires the judgment silently)
  9. self-reference-- every path-filtered workflow's `paths` covers its own YAML
 10. manual dispatch-- every workflow is manually dispatchable

Deliberate omission: each workflow's own YAML file is covered by a footnoted
sentence in the README instead of a table cell, so it is dropped from the YAML
side before comparing. That subtraction is only sound while the element is really
there -- judgment 9 asserts exactly that, so the premise is a checked condition
rather than an assumption recorded in a comment. Judgments 8-10 cover the rest of
the CI section's hand-written claims: the prose count and the footnote's two
halves. All three read the same closed input set; none of them adds a dependency.

Each judgment emits its own message so an ablation can pin them one at a time:
a guard that has never gone red is not evidence, and a set of mutations that all
report the same message may be pinning only one of them.

Usage: check-readme-ci-table.py
Exit code IS the verdict (unlike `check-spec-refs.py --audit`, which is advisory).
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
WF_DIR = REPO / ".github" / "workflows"

# The READMEs holding the table, mapped to the separator their path cell uses.
READMES = {
    "README.md": ", ",
    "README.zh-CN.md": "、",
}

# Non-vacuity floors. These exist because the README half is parsed by hand: if
# the table were ever reformatted past the parser, the parser would quietly
# return nothing and this guard would go green having looked at nothing. A guard
# that cannot fail is worse than no guard, so "I saw something" is assertion #1.
MIN_ROWS = 1
MIN_WORKFLOWS = 1

EXPECTED_BRANCHES = ["main"]

WF_IN_CELL = re.compile(r"`([A-Za-z0-9._/-]+\.yml)`")

# Judgment 8 vocabulary, bounded on purpose: an unbounded scan would eat an
# unrelated number out of the prose and pin the wrong thing.
COUNT_PATTERNS = {
    "README.md": re.compile(r"([A-Za-z]+|\d+)\s+independent workflows\b"),
    "README.zh-CN.md": re.compile(
        r"([一二三四五六七八九十]{1,2})"
        r"\s*套\s*独立工作流"
    ),
}
COUNT_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12,
}
COUNT_CJK = {
    "\u4e00": 1, "\u4e8c": 2, "\u4e09": 3, "\u56db": 4, "\u4e94": 5,
    "\u516d": 6, "\u4e03": 7, "\u516b": 8, "\u4e5d": 9, "\u5341": 10,
    "\u5341\u4e00": 11, "\u5341\u4e8c": 12,
}


def pattern_covers(pattern: str, path: str) -> bool:
    """Does one `paths` entry select `path`?

    GitHub matches these as globs. Only the two shapes this repo actually uses
    matter for judgment 9 -- an exact path, and `prefix/**` -- but `*` within a
    segment is honoured too so a legitimate `.github/workflows/*.yml` is not
    reported as a violation. A shape the matcher cannot express returns False,
    which surfaces as a loud judgment-9 failure rather than a silent pass.
    """
    if pattern == path:
        return True
    out = []
    for i, seg in enumerate(pattern.split("/")):
        if i:
            out.append("/")
        if seg == "**":
            out.append(".*")
        else:
            out.append(re.escape(seg).replace(r"\*", "[^/]*"))
    return re.match("^" + "".join(out) + "$", path) is not None


def workflow_count(section: str, name: str) -> tuple[int, str] | None:
    """The count the CI prose claims, as (value, token), or None if it says none.

    Returning None is a FAILURE at the call site: if the sentence is reworded past
    this parser, the judgment must go red, not quiet.
    """
    m = COUNT_PATTERNS[name].search(section)
    if not m:
        return None
    token = m.group(1)
    if token.isdigit():
        return int(token), token
    # English number words arrive capitalised at sentence start ("Nine"), so the
    # lookup folds case -- the README claims a count, not a case.
    if name.endswith("zh-CN.md"):
        if token in COUNT_CJK:
            return COUNT_CJK[token], token
    elif token.lower() in COUNT_WORDS:
        return COUNT_WORDS[token.lower()], token
    return None


def ci_section(text: str) -> str:
    """The body of the `## CI` section, up to the next `## ` heading."""
    start = text.find("## CI")
    if start < 0:
        return ""
    rest = text[start + len("## CI") :]
    nxt = rest.find("\n## ")
    return rest[:nxt] if nxt >= 0 else rest


def parse_paths_cell(cell: str, sep: str) -> set[str] | None:
    """Paths claimed by one row, or None when the row says "no filter"."""
    cell = cell.strip()
    if not cell or cell.startswith("*"):
        return None
    return {p.strip().strip("`").strip() for p in cell.split(sep) if p.strip()}


def parse_rows(section: str, sep: str) -> list[tuple[str, set[str] | None]]:
    """Rows of the table as (workflow file, claimed paths or None)."""
    rows: list[tuple[str, set[str] | None]] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        m = WF_IN_CELL.search(cells[0])
        if not m:
            continue
        rows.append((m.group(1), parse_paths_cell(cells[1], sep)))
    return rows


def workflow_facts(path: pathlib.Path) -> dict:
    """What one workflow file actually says about its own triggers.

    `yaml.safe_load` parses the bare key `on:` as the boolean True (PyYAML 1.1),
    so both spellings are looked up.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    on = data.get("on")
    if on is None:
        on = data.get(True)
    on = on or {}

    def side(key: str) -> tuple[list[str] | None, list[str] | None]:
        block = on.get(key) or {}
        if not isinstance(block, dict):
            return None, None
        return block.get("paths"), block.get("branches")

    push_paths, push_branches = side("push")
    pr_paths, pr_branches = side("pull_request")
    return {
        "push_paths": push_paths,
        "pr_paths": pr_paths,
        "push_branches": push_branches,
        "pr_branches": pr_branches,
        "dispatch": isinstance(on, dict) and "workflow_dispatch" in on,
    }


def main() -> int:
    problems: list[str] = []

    on_disk = sorted(p.name for p in WF_DIR.glob("*.yml"))
    if len(on_disk) < MIN_WORKFLOWS:
        problems.append(f"found only {len(on_disk)} workflow file(s); expected >= {MIN_WORKFLOWS}")

    facts = {name: workflow_facts(WF_DIR / name) for name in on_disk}

    # judgments 9/10 -- properties of the YAML alone, so they are checked once
    # rather than once per README (which would double every message).
    for wf in on_disk:
        push_paths = facts[wf]["push_paths"]
        # judgment 9 -- the footnote says a filtered workflow's own YAML matches
        # its own `paths`. This is also what makes the subtraction below sound.
        if push_paths is not None:
            self_path = f".github/workflows/{wf}"
            if not any(pattern_covers(p, self_path) for p in push_paths):
                problems.append(
                    f"{wf}: paths no longer covers its own workflow file; "
                    "the README footnote claims it does"
                )
        # judgment 10 -- the footnote's other half
        if not facts[wf]["dispatch"]:
            problems.append(
                f"{wf}: no workflow_dispatch trigger, but the README says every "
                "workflow supports manual dispatch"
            )

    for name, sep in READMES.items():
        path = REPO / name
        section = ci_section(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        rows = parse_rows(section, sep)
        if len(rows) < MIN_ROWS:
            problems.append(f"{name}: parsed {len(rows)} CI row(s); expected >= {MIN_ROWS}")
            continue

        # judgment 8 -- the prose's own count. Absent count is a failure: a
        # reworded sentence must redden this guard, not retire it quietly.
        claimed_count = workflow_count(section, name)
        if claimed_count is None:
            problems.append(f"{name}: no workflow count found in the CI prose")
        elif claimed_count[0] != len(on_disk):
            problems.append(
                f"{name}: prose says {claimed_count[1]} workflow(s), "
                f"but {len(on_disk)} exist"
            )

        claimed_by = {}
        for wf, claimed in rows:
            if wf in claimed_by:
                problems.append(f"{name}: duplicate row for `{wf}`")
            claimed_by[wf] = claimed

        # judgment 2 -- existence, both directions
        missing = sorted(set(on_disk) - set(claimed_by))
        extra = sorted(set(claimed_by) - set(on_disk))
        if missing:
            problems.append(f"{name}: workflows on disk but not in table: {missing}")
        if extra:
            problems.append(f"{name}: table names workflows that do not exist: {extra}")

        for wf in sorted(set(claimed_by) & set(on_disk)):
            f = facts[wf]
            push_paths, pr_paths = f["push_paths"], f["pr_paths"]
            label = f"{name}: {wf}"

            # judgment 3 -- symmetry
            if push_paths != pr_paths:
                problems.append(
                    f"{label}: push/pull_request paths differ; "
                    "one row cannot describe both"
                )

            # judgments 5/6 -- the filter shape has to agree before comparing contents
            claimed = claimed_by[wf]
            if push_paths is None:
                if claimed:
                    problems.append(
                        f"{label}: no paths filter, but the row lists "
                        f"{sorted(claimed)}"
                    )
                continue
            if claimed is None:
                problems.append(f"{label}: has a paths filter, but the row claims none")
                continue

            if push_paths is not None:
                truth = set(push_paths) - {f".github/workflows/{wf}"}
                # judgment 4 -- no fabrication (this is the one T36 hit)
                bogus = sorted(claimed - truth)
                if bogus:
                    problems.append(f"{label}: row claims paths the YAML does not have: {bogus}")
                # judgment 5 -- no omission
                omitted = sorted(truth - claimed)
                if omitted:
                    problems.append(f"{label}: YAML has paths the row omits: {omitted}")

            # judgment 7 -- the prose says every workflow targets `main`
            for side_ in ("push_branches", "pr_branches"):
                got = f[side_]
                if got != EXPECTED_BRANCHES:
                    problems.append(
                        f"{label}: {side_.replace('_branches', '')} branches are {got}, "
                        f"but the README says all workflows target {EXPECTED_BRANCHES}"
                    )

    print(f"readme ci table: {len(on_disk)} workflow(s) reconciled in {len(READMES)} file(s)")
    if problems:
        print("FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("readme-ci-table check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
