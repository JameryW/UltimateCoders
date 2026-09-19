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

Deliberate omission: each workflow's own YAML file IS in its `paths` but is
covered by a footnoted sentence in the README instead of a table cell, so it is
dropped from the YAML side before comparing.

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
    }


def main() -> int:
    problems: list[str] = []

    on_disk = sorted(p.name for p in WF_DIR.glob("*.yml"))
    if len(on_disk) < MIN_WORKFLOWS:
        problems.append(f"found only {len(on_disk)} workflow file(s); expected >= {MIN_WORKFLOWS}")

    facts = {name: workflow_facts(WF_DIR / name) for name in on_disk}

    for name, sep in READMES.items():
        path = REPO / name
        section = ci_section(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        rows = parse_rows(section, sep)
        if len(rows) < MIN_ROWS:
            problems.append(f"{name}: parsed {len(rows)} CI row(s); expected >= {MIN_ROWS}")
            continue

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
