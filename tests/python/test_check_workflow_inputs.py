"""Tests for scripts/check-workflow-inputs.py.

T35 / #685 established that the inputs a gate depends on must all be listed in
that gate's `paths`, and T36 / #686 applied the rule once, by hand, to
`ci-python.yml`. `scripts/check-workflow-inputs.py` makes it mechanical. Three
things are pinned here:

  * the real repo passes, and the guard reports what it looked at -- a
    non-vacuity check from the outside, so a guard that quietly parses nothing
    cannot pass by being quiet;
  * every branch that can report a problem is reached by at least one mutation.
    A set of mutations that all redden the same judgment may be pinning only one
    of them, so coverage of the judgment list is asserted, not the count;
  * the two hand-written components -- the `paths` matcher and the `run:`
    reference extractor -- carry positive AND negative controls, and the matcher
    agrees with the copy `check-readme-ci-table.py` carries.

MEASURED, NOT ASSUMED
---------------------
The first `covered_by` in the guard used `fnmatch` plus a `prefix/**` shortcut.
Compared against the README guard's matcher it disagreed on exactly two inputs:
`('scripts/*.py', 'scripts/sub/a.py')` (fnmatch's `*` crosses `/`) and
`('scripts/**', 'scriptsX/a.py')` (the prefix checked no boundary). Both would
have made judgments 2/3 here disagree with the README guard about the same YAML,
so the matcher was corrected, and `test_the_two_path_matchers_agree` now pins the
two copies together.

ANCHORS ARE MATCHED WITH AN EXPECTED COUNT
------------------------------------------
Same rule as `test_check_readme_ci_table.py`: a plain first-occurrence replace can
silently mutate a header comment instead of the thing under test, leaving the
guard green and the mutation meaningless. Every anchor below states how many times
it should occur, and a multi-line anchor is rewritten to the target file's own
line ending (these workflow files are CRLF).
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "check-workflow-inputs.py"
README_GUARD = REPO / "scripts" / "check-readme-ci-table.py"
PY = sys.executable

WORKFLOWS = ".github/workflows"

# Every message the guard can report, with a substring that identifies it. The
# first three are the non-vacuity family: they exist so a guard that looks at
# nothing fails loudly instead of passing quietly.
JUDGMENTS = {
    "non-vacuity-workflows": "parsed 0 workflow(s)",
    "non-vacuity-filtered": "no path-filtered workflow to check",
    "non-vacuity-references": "extracted 0 run-step reference(s)",
    "dead-filter-push": "push paths filter is empty",
    "dead-filter-pr": "pull_request paths filter is empty",
    "push-coverage": "push paths do not cover",
    "pr-coverage": "pull_request paths do not cover",
}

# List items carry their indentation and quotes, so a header comment that mentions
# the same path in prose can never absorb a mutation.
ITEM = '      - "{path}"'


def build_sandbox(tmp_path: pathlib.Path) -> pathlib.Path:
    """A standalone copy of the repo shaped exactly like it.

    The guard derives its root from `__file__/../..`, so copying the script to
    `<sandbox>/scripts/` is enough to point it at the sandbox -- there is no
    test-only hook in the production code. Its input set is closed to
    `.github/workflows/*.yml`, so nothing else has to be copied.
    """
    sb = tmp_path / "sb"
    (sb / WORKFLOWS).mkdir(parents=True)
    for wf in (REPO / WORKFLOWS).glob("*.yml"):
        shutil.copy2(wf, sb / WORKFLOWS / wf.name)
    (sb / "scripts").mkdir()
    shutil.copy2(GUARD, sb / "scripts" / GUARD.name)
    return sb


def synthetic_sandbox(tmp_path: pathlib.Path, name: str,
                      files: dict[str, str]) -> pathlib.Path:
    """A sandbox whose workflows directory holds only the given files."""
    sb = tmp_path / name
    (sb / WORKFLOWS).mkdir(parents=True)
    for fname, text in files.items():
        (sb / WORKFLOWS / fname).write_bytes(text.encode("utf-8"))
    (sb / "scripts").mkdir()
    shutil.copy2(GUARD, sb / "scripts" / GUARD.name)
    return sb


def run(sb: pathlib.Path) -> tuple[int, str]:
    r = subprocess.run(
        [PY, str(sb / "scripts" / GUARD.name)],
        capture_output=True,
        cwd=str(sb),
    )
    return r.returncode, r.stdout.decode("utf-8", "replace")


def problems(out: str) -> list[str]:
    return [line.strip().lstrip("- ").strip() for line in out.splitlines()
            if line.strip().startswith("- ")]


def mutate(sb: pathlib.Path, rel: str, old: str, new: str, expect: int,
           how: str = "first") -> bytes:
    """Apply one mutation, asserting the anchor occurs exactly `expect` times.

    `how` is "first", "last", or "all". "last" exists because the two `paths`
    blocks in a workflow are byte-identical, so the only way to mutate just the
    `pull_request` side is to take the last occurrence.
    """
    p = sb / rel
    b = p.read_bytes()
    if "\n" in old:
        nl = b"\r\n" if b.count(b"\r\n") else b"\n"
        old = old.replace("\n", nl.decode())
        new = new.replace("\n", nl.decode())
    o, n = old.encode(), new.encode()
    got = b.count(o)
    assert got == expect, f"{rel}: anchor occurs {got}x, expected {expect}: {old[:70]!r}"
    if how == "all":
        out = b.replace(o, n)
    else:
        assert expect >= 1
        idx = b.find(o) if how == "first" else b.rfind(o)
        assert idx >= 0, f"{rel}: {how} occurrence not found"
        out = b[:idx] + n + b[idx + len(o):]
    p.write_bytes(out)
    return b


def restore(sb: pathlib.Path, rel: str, original: bytes) -> None:
    p = sb / rel
    p.write_bytes(original)
    assert p.read_bytes() == original, f"{rel}: restore failed"


def test_real_repo_is_reconciled() -> None:
    """The guard passes on the real repo, and says what it looked at.

    The summary line is pinned on purpose: adding a workflow file or a reference
    in a `run:` step has to move this number in the same change, the way the
    corpus pin in `test_check_tasks_refs.py` already behaves.
    """
    r = subprocess.run([PY, str(GUARD)], capture_output=True, cwd=str(REPO))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, f"guard failed on the real repo:\n{out}"
    n = len(list((REPO / WORKFLOWS).glob("*.yml")))
    assert f"{n} workflow(s), " in out, (
        f"the guard must report how much it looked at; got:\n{out}"
    )
    expected = (
        "workflows: 10 workflow(s), 9 path-filtered, 18 run-step reference(s), "
        "13 subject to coverage"
    )
    assert expected in out, f"the reported summary has moved; got:\n{out}"


def test_every_judgment_is_pinned_by_a_mutation(tmp_path) -> None:
    sb = build_sandbox(tmp_path)
    rc, out = run(sb)
    assert rc == 0, f"sandbox baseline must be green:\n{out}"

    python_wf = f"{WORKFLOWS}/ci-python.yml"
    dashboard = f"{WORKFLOWS}/ci-dashboard.yml"
    py_paths_item = ITEM.format(path="crates/uc-python/**")
    dash_block = (
        "    paths:\n"
        '      - "dashboard/**"\n'
        '      - ".github/workflows/ci-dashboard.yml"\n'
    )

    # (label, judgment it pins, file, old, new, expected occurrences, how)
    cases = [
        (
            "A push paths lose a file a run step names (the T36 shape)",
            "push-coverage", python_wf, py_paths_item, "", 2, "first",
        ),
        (
            "B pull_request paths lose the same file",
            "pr-coverage", python_wf, py_paths_item, "", 2, "last",
        ),
        (
            "C push paths become an empty filter",
            "dead-filter-push", dashboard, dash_block, "    paths: []\n", 2, "first",
        ),
        (
            "D pull_request paths become an empty filter",
            "dead-filter-pr", dashboard, dash_block, "    paths: []\n", 2, "last",
        ),
    ]

    covered: dict[str, str] = {}
    for label, judgment, rel, old, new, expect, how in cases:
        original = (sb / rel).read_bytes()
        mutate(sb, rel, old, new, expect, how)
        rc, out = run(sb)
        restore(sb, rel, original)
        assert rc != 0, f"{label}: expected the guard to redden"
        msgs = problems(out)
        assert msgs, f"{label}: reddened but reported no problem line"
        assert any(JUDGMENTS[judgment] in m for m in msgs), (
            f"{label}: expected the {judgment!r} judgment, got {msgs}"
        )
        covered[judgment] = label

    # The non-vacuity family guards against the guard looking at nothing, so it is
    # reached by degenerate INPUT rather than by mutating the real corpus -- which
    # is also why each gets its own minimal sandbox.
    empty = synthetic_sandbox(tmp_path, "empty", {})
    rc, out = run(empty)
    assert rc != 0, out
    assert JUDGMENTS["non-vacuity-workflows"] in out, out
    covered["non-vacuity-workflows"] = "an empty workflows directory"

    unfiltered = synthetic_sandbox(tmp_path, "unfiltered", {
        "x.yml": (
            "on:\n  push:\n\njobs:\n  j:\n    steps:\n"
            "      - run: python scripts/x.py\n"
        ),
    })
    rc, out = run(unfiltered)
    assert rc != 0, out
    assert JUDGMENTS["non-vacuity-filtered"] in out, out
    covered["non-vacuity-filtered"] = "no workflow carries a paths filter"

    no_refs = synthetic_sandbox(tmp_path, "norefs", {
        "x.yml": (
            "on:\n  push:\n    paths:\n      - \"scripts/**\"\n\n"
            "jobs:\n  j:\n    steps:\n      - run: echo hello\n"
        ),
    })
    rc, out = run(no_refs)
    assert rc != 0, out
    assert JUDGMENTS["non-vacuity-references"] in out, out
    covered["non-vacuity-references"] = "the extractor finds nothing"

    missing = sorted(set(JUDGMENTS) - set(covered))
    assert not missing, f"judgments no mutation reaches: {missing}"


def test_the_guard_does_not_demand_a_filter(tmp_path) -> None:
    """Negative controls: two shapes that must stay green.

    Judgment 2/3 is "an input a gate names must be covered", not "every workflow
    must be filtered". A guard that reddened on either of these would be reporting
    a policy nobody agreed to, so both directions are pinned.
    """
    sb = build_sandbox(tmp_path)

    # 1. A workflow loses its `paths` filter: it now runs on every change, so
    #    nothing can be missed. Its references stop being subject to judgment 2/3.
    journal = f"{WORKFLOWS}/ci-journal.yml"
    journal_block = (
        "    paths:\n"
        '      - ".trellis/workspace/**"\n'
        '      - ".trellis/scripts/add_session.py"\n'
        '      - "scripts/check-journal-ledger.py"\n'
        '      - "tests/python/test_check_journal_ledger.py"\n'
        '      - ".github/workflows/ci-journal.yml"\n'
    )
    original = (sb / journal).read_bytes()
    mutate(sb, journal, journal_block, "", 2, "first")
    rc, out = run(sb)
    restore(sb, journal, original)
    assert rc == 0, f"an unfiltered workflow must not be a violation:\n{out}"

    # 2. A workflow that listens to neither `push` nor `pull_request` (dashboard
    #    only has `workflow_dispatch` left) is not this guard's business at all.
    dashboard = f"{WORKFLOWS}/ci-dashboard.yml"
    both_events = (
        "  push:\n    branches: [main]\n    paths:\n"
        '      - "dashboard/**"\n'
        '      - ".github/workflows/ci-dashboard.yml"\n'
        "  pull_request:\n    branches: [main]\n    paths:\n"
        '      - "dashboard/**"\n'
        '      - ".github/workflows/ci-dashboard.yml"\n'
    )
    original = (sb / dashboard).read_bytes()
    mutate(sb, dashboard, both_events, "", 1, "first")
    rc, out = run(sb)
    restore(sb, dashboard, original)
    assert rc == 0, f"a dispatch-only workflow must not be a violation:\n{out}"

    # ... and the sandbox is back where it started.
    assert run(sb)[0] == 0


def _load(path: pathlib.Path, name: str):
    """Import a hyphenated script as a module so its parsers can be pinned."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parsers_are_pinned_both_ways() -> None:
    """Judgments 2/3 rest on two hand-written components, so pin both directions.

    A matcher that always returns True makes them permanently green, and an
    extractor that always returns nothing makes them vacuous. Positive controls
    alone cannot tell those apart (T33 / #683: a positive control needs its own
    ablation), so every row below has a negative twin.

    The first negative of the matcher table is the measured one: `fnmatch` says
    True for it, which is what the first draft used.
    """
    guard = _load(GUARD, "check_workflow_inputs")

    covers = [
        (".github/workflows/**", ".github/workflows/ci-rust.yml", True),
        ("crates/uc-python/**", "crates/uc-python/Cargo.toml", True),
        ("tests/python/test_x.py", "tests/python/test_x.py", True),
        (".github/workflows/*.yml", ".github/workflows/ci-rust.yml", True),
        ("scripts/*.py", "scripts/sub/a.py", False),
        ("scripts/**", "scriptsX/a.py", False),
        ("dashboard/**", "docs/dashboard/x.py", False),
    ]
    for pattern, path, expected in covers:
        assert guard.covered_by(pattern, path) is expected, (
            f"covered_by({pattern!r}, {path!r}) should be {expected}"
        )

    refs = [
        ("python scripts/check-x.py", {"scripts/check-x.py"}),
        ("./tests/python/test_a.py", {"tests/python/test_a.py"}),
        ("maturin build --manifest-path crates/uc-python/Cargo.toml",
         {"crates/uc-python/Cargo.toml"}),
        ("curl https://example.com/scripts/x.py", set()),
        ("node tools/build.js", set()),
        ("echo 'no file here'", set()),
    ]
    for body, expected in refs:
        got = guard.references([body])
        assert got == expected, f"references({body!r}) = {got}, want {expected}"

    # The extractor runs only over `run:` steps, with shell comments dropped. This
    # is the half that a repo-wide token scan gets wrong: a path in a comment is
    # not an input (T39's own header comments name `.trellis/.template-hashes.json`).
    bodies = guard.run_texts({"jobs": {"j": {"steps": [
        {"run": "# see scripts/ghost.py\necho ok"},
        {"uses": "actions/checkout@v4"},
        {"run": "python -m pytest tests/python/real.py"},
    ]}}})
    assert bodies == ["echo ok", "python -m pytest tests/python/real.py"], bodies
    assert guard.references(bodies) == {"tests/python/real.py"}


def test_the_two_path_matchers_agree() -> None:
    """The two guards carry the same `paths` matcher, as two copies on purpose.

    They are not one shared import because a shared module would become an input
    of both guards, and neither guard's `paths` may name the other's file -- so
    sharing would quietly resurrect the very bug this ticket exists to catch. Two
    copies need a test that they still mean the same thing, which is this one.

    The table must also contain both verdicts: two matchers that always return
    False would "agree" perfectly.
    """
    here = _load(GUARD, "check_workflow_inputs")
    there = _load(README_GUARD, "check_readme_ci_table")

    cases = [
        (".github/workflows/**", ".github/workflows/ci-rust.yml"),
        (".github/workflows/*.yml", ".github/workflows/ci-rust.yml"),
        ("crates/uc-python/**", "crates/uc-python/Cargo.toml"),
        ("scripts/*.py", "scripts/a.py"),
        ("tests/python/test_x.py", "tests/python/test_x.py"),
        ("scripts/*.py", "scripts/sub/a.py"),
        ("scripts/**", "scriptsX/a.py"),
        ("scripts/**", "scripts"),
        ("dashboard/**", "docs/dashboard/x.py"),
        ("docs/agents/*.md", "docs/agents/nested/domain.md"),
    ]
    verdicts = {here.covered_by(p, q) for p, q in cases}
    assert verdicts == {True, False}, f"the table is vacuous: only {verdicts}"

    for pattern, path in cases:
        a = here.covered_by(pattern, path)
        b = there.pattern_covers(pattern, path)
        assert a is b, f"matchers disagree on ({pattern!r}, {path!r}): {a} vs {b}"


def test_mutations_are_independent(tmp_path) -> None:
    """Restoring really restores -- otherwise case N+1 is testing mutation N."""
    sb = build_sandbox(tmp_path)
    assert run(sb)[0] == 0
    original = (sb / WORKFLOWS / "ci-python.yml").read_bytes()
    mutate(sb, f"{WORKFLOWS}/ci-python.yml",
           ITEM.format(path="crates/uc-python/**"), "", 2, "first")
    assert run(sb)[0] != 0
    restore(sb, f"{WORKFLOWS}/ci-python.yml", original)
    assert run(sb)[0] == 0
