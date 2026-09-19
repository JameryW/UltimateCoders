#!/usr/bin/env python3
"""Check that a workflow's `run:` steps are covered by that workflow's `paths`.

T35 / #685 established the rule -- **the inputs a gate depends on must all be
listed in its `paths`** -- and T36 / #686 applied it once, by hand, to
`ci-python.yml`'s test job. The measurement behind that ticket is the reason this
file exists: the job compiles `crates/uc-python` with maturin while its `paths`
named no `crates/**` entry at all, so **43 of 218 pushes (about 20%)** let the
Python-side engine check silently not run. Nothing reddens when a gate does not
run -- that is the whole failure mode.

A `run:` step names its inputs by relative path, so the rule can be checked
mechanically. For each workflow:

  1. non-vacuity   -- workflows were parsed and references were extracted at all
  2. push coverage -- every named file is selected by `on.push.paths`
  3. PR coverage   -- ... and by `on.pull_request.paths`
  4. dead filter   -- a `paths` filter that exists but is empty never triggers

Judgment 4 is the limiting case of 2/3 (`paths: []` covers nothing) and is
reported separately, so a mutation there reddens one message rather than two.

WHY THE EXTRACTION IS STRUCTURAL AND COMMENT-AWARE.
Reading the whole YAML for path-shaped tokens does not work, and the failure is
not hypothetical -- measured on this repo, that approach reports
`.trellis/.template-hashes.json` (three hits, all inside header comments) and
`docs/agents/*.md` (a glob that only ever existed in prose; T36 caught it being
invented for a README table). A comment is not an input. So the extractor walks
`jobs.*.steps[*].run`, drops shell comment lines, and only then looks for
references.

WHY `TOP_LEVEL_DIRS` IS DECLARED AND NOT DERIVED.
Deriving it from the file tree would make this guard read the whole repository,
and a reader whose input set is the whole repo must NOT be path-filtered -- a
`paths` filter over it would hide exactly the edits that matter (T35 / #685).
Declaring the list keeps the input set closed to `.github/workflows/*.yml`, at
the cost that a new top-level directory has to be added here in the same change
that references it. The constant is printed, so an omission is visible, and the
test pins its contents.

Same reasoning for what is deliberately not checked: `docker/docker-compose.yml`
mounts `./tikv.toml`, `./nats.conf` and `../`, so its transitive input set is the
repository root -- not expressible as a `paths` entry. That is recorded, not
attempted.

Each judgment emits its own message so an ablation can pin them one at a time:
a guard that has never gone red is not evidence, and a set of mutations that all
report the same message may be pinning only one of them.

Usage: check-workflow-inputs.py
Exit code IS the verdict.
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ".github/workflows"

# See the module docstring: declared, not derived. The first segment of every
# `run:`-named repository path has to be one of these.
TOP_LEVEL_DIRS = (
    ".agents",
    ".github",
    ".trellis",
    "crates",
    "dashboard",
    "docker",
    "docs",
    "packages",
    "python",
    "scripts",
    "tests",
    "vendor",
)

FILE_SUFFIXES = (
    "conf", "json", "md", "py", "rs", "sh", "toml", "ts", "tsx", "yaml", "yml",
)

# A reference is `<top-level dir>/<...>/<name>.<known suffix>`. The lookbehind
# rejects a token that continues a longer path or a URL (`.../scripts/x.py`);
# `./scripts/x.py` is normalised away before matching, and `../scripts/x.py`
# becomes `.scripts/...`, which no longer starts with a known directory.
REFERENCE = re.compile(
    r"(?<![\w:/])(?:" + "|".join(re.escape(d) for d in TOP_LEVEL_DIRS) + r")"
    r"/[\w./*-]+\.(?:" + "|".join(FILE_SUFFIXES) + r")(?![\w])"
)

SHELL_COMMENT = re.compile(r"^\s*#")

MIN_WORKFLOWS = 1
MIN_FILTERED = 1
MIN_REFERENCES = 1


def load(path: pathlib.Path) -> tuple[dict, dict]:
    """Parsed YAML and the `on:` mapping.

    A bare `on:` is parsed as the boolean True by PyYAML (YAML 1.1), so the
    literal key has to be tried too.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    on = data.get("on")
    if on is None:
        on = data.get(True)
    return data, (on if isinstance(on, dict) else {})


def run_texts(data: dict) -> list[str]:
    """Every `run:` script body, with shell comment lines dropped."""
    bodies = []
    for job in (data.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                bodies.append("\n".join(
                    line for line in step["run"].splitlines()
                    if not SHELL_COMMENT.match(line)
                ))
    return bodies


def references(bodies: list[str]) -> set[str]:
    """Repository paths named by `run:` steps."""
    found: set[str] = set()
    for body in bodies:
        found |= set(REFERENCE.findall(body.replace("./", "")))
    return found


def event_paths(on: dict, event: str) -> tuple[list[str] | None, bool]:
    """`(paths, triggered)` for one event.

    `paths is None` means the event runs unfiltered -- either it has no `paths`
    key, or it is declared with an empty body. `triggered` is False when the
    workflow does not listen for the event at all.
    """
    if event not in on:
        return None, False
    cfg = on[event]
    if isinstance(cfg, dict):
        return cfg.get("paths"), True
    return None, True


def covered_by(entry: str, path: str) -> bool:
    """Does one `paths` entry select `path`?

    GitHub matches a `paths` entry as a segment-scoped glob: `**` spans `/`, but
    `*` does not, so `scripts/*.py` selects `scripts/a.py` and deliberately not
    `scripts/sub/a.py`. The first attempt here used `fnmatch`, whose `*` *does*
    cross `/`, and a `prefix/**` shortcut whose prefix had no boundary check --
    measured, those two disagreed with the matcher below on exactly those two
    inputs. A matcher is load-bearing for judgments 2/3, so it has to be the
    same contract, not merely a similar-looking one.

    `check-readme-ci-table.py` carries the same logic under the name
    `pattern_covers`; `test_the_two_path_matchers_agree` pins them together on a
    truth table. They are kept as two copies rather than one import because a
    shared module would become an input of both guards, and each guard's `paths`
    is not allowed to name the other's -- so sharing would silently resurrect
    the bug this ticket exists to catch.
    """
    if entry == path:
        return True
    out = []
    for i, seg in enumerate(entry.split("/")):
        if i:
            out.append("/")
        if seg == "**":
            out.append(".*")
        else:
            out.append(re.escape(seg).replace(r"\*", "[^/]*"))
    return re.match("^" + "".join(out) + "$", path) is not None


def uncovered(paths: list[str], refs: set[str]) -> list[str]:
    return sorted(r for r in refs if not any(covered_by(p, r) for p in paths))


def describe(paths: list[str] | None, triggered: bool) -> str:
    if not triggered:
        return "off"
    if paths is None:
        return "none"
    return f"paths({len(paths)})"


def main() -> int:
    files = sorted((REPO / WORKFLOW_DIR).glob("*.yml"))
    if len(files) < MIN_WORKFLOWS:
        print(f"error: parsed {len(files)} workflow(s) from {WORKFLOW_DIR}/")
        return 1

    problems: list[str] = []
    parsed: list[tuple[str, str, str, int]] = []
    filtered = 0
    total_refs = 0
    checked_refs = 0

    for path in files:
        name = path.name
        data, on = load(path)
        refs = references(run_texts(data))
        push_paths, push_on = event_paths(on, "push")
        pr_paths, pr_on = event_paths(on, "pull_request")

        total_refs += len(refs)
        if push_paths is not None:
            filtered += 1
        parsed.append((name, describe(push_paths, push_on),
                       describe(pr_paths, pr_on), len(refs)))

        # judgment 4 -- a filter that selects nothing is a gate that never runs
        if push_paths is not None and not push_paths:
            problems.append(
                f"{name}: push paths filter is empty, so this workflow can never trigger"
            )
            continue
        if pr_paths is not None and not pr_paths:
            problems.append(
                f"{name}: pull_request paths filter is empty, so this workflow can never trigger"
            )
            continue
        if not push_on and not pr_on:
            continue

        # judgments 2/3 -- the inputs a gate names must be in its trigger set
        subject = False
        for event, paths, triggered in (("push", push_paths, push_on),
                                        ("pull_request", pr_paths, pr_on)):
            if not triggered:
                continue
            if paths is None:
                continue  # unfiltered: it runs on every change, so nothing can be missed
            subject = True
            for ref in uncovered(paths, refs):
                problems.append(f"{name}: {event} paths do not cover {ref}")
        if subject:
            checked_refs += len(refs)

    width = max((len(r[0]) for r in parsed), default=0)
    for name, push, pr, n in parsed:
        print(f"  {name:<{width}}  push={push:<10} pr={pr:<10} refs={n}")
    print(f"TOP_LEVEL_DIRS={len(TOP_LEVEL_DIRS)}: {', '.join(TOP_LEVEL_DIRS)}")

    if filtered < MIN_FILTERED:
        print(f"error: no path-filtered workflow to check (found {filtered})")
        return 1
    if total_refs < MIN_REFERENCES:
        print(f"error: extracted {total_refs} run-step reference(s) from "
              f"{len(parsed)} workflow(s)")
        return 1

    print(f"workflows: {len(parsed)} workflow(s), {filtered} path-filtered, "
          f"{total_refs} run-step reference(s), {checked_refs} subject to coverage")

    if problems:
        for p in problems:
            print(f"- {p}")
        return 1
    print("workflow inputs check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
