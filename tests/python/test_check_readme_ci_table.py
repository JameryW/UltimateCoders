"""Tests for scripts/check-readme-ci-table.py.

Two things are pinned here:

  * the real repo passes, and the guard reports having looked at both READMEs
    and every workflow -- a non-vacuity check from the outside, so a guard that
    quietly parses nothing cannot pass by being quiet;
  * every judgment inside the guard is reached by at least one mutation;
  * the two hand-written parsers (the `paths` matcher and the prose-count
    reader) are pinned with positive AND negative controls.

A guard that has never gone red is not evidence, and a set of mutations that all
report the same message may be pinning only one branch. So each mutation is
applied alone to a sandbox copy (the real repo is never touched), each is
byte-restored, and the collected failure messages are checked for coverage of
the judgment list -- not merely for a non-zero count.

ANCHORS ARE MATCHED WITH AN EXPECTED COUNT
------------------------------------------
`ci-trellis.yml` opens with a long header comment that mentions
`.trellis/scripts/**` in prose. A plain first-occurrence replace therefore
mutated the comment and the guard stayed green -- the mutation had no effect and
only the "it must redden" assertion caught it. Every anchor below now states how
many times it should occur, so a wrong or ambiguous anchor fails loudly instead
of silently doing nothing.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "check-readme-ci-table.py"
PY = sys.executable

README = "README.md"
README_ZH = "README.zh-CN.md"
WORKFLOWS = ".github/workflows"

# Every judgment the guard can report, with a substring that identifies it.
JUDGMENTS = {
    "non-vacuity": "parsed 0 CI row(s)",
    "existence": "table names workflows that do not exist",
    "symmetry": "push/pull_request paths differ",
    "fabrication": "row claims paths the YAML does not have",
    "omission": "YAML has paths the row omits",
    "filter-shape": "no paths filter, but the row lists",
    "branches": "branches are",
    "prose-count": "prose says",
    "prose-count-missing": "no workflow count found",
    "self-reference": "paths no longer covers its own workflow file",
    "manual-dispatch": "no workflow_dispatch trigger",
}

# List items are matched with their indentation and quotes so the header
# comments (which mention the same paths in prose) can never absorb a mutation.
ITEM = '      - "{path}"'


def build_sandbox(tmp_path: pathlib.Path) -> pathlib.Path:
    """A standalone copy of the repo shaped exactly like it.

    The guard derives its root from `__file__/../..`, so copying the script to
    `<sandbox>/scripts/` is enough to point it at the sandbox -- no test-only
    hook in the production code.
    """
    sb = tmp_path / "sb"
    (sb / WORKFLOWS).mkdir(parents=True)
    for wf in (REPO / WORKFLOWS).glob("*.yml"):
        shutil.copy2(wf, sb / WORKFLOWS / wf.name)
    for name in (README, README_ZH):
        shutil.copy2(REPO / name, sb / name)
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
    """Apply one mutation, asserting the anchor occurs exactly `expect` times."""
    p = sb / rel
    b = p.read_bytes()
    if "\n" in old:
        # A multi-line anchor has to use the file's own line ending. These
        # workflow files are CRLF, so a "\n" anchor matches zero times and --
        # without the count assertion -- would silently mutate nothing.
        nl = b"\r\n" if b.count(b"\r\n") else b"\n"
        old = old.replace("\n", nl.decode())
        new = new.replace("\n", nl.decode())
    n = b.count(old.encode())
    assert n == expect, f"{rel}: anchor occurs {n}x, expected {expect}: {old[:60]!r}"
    if how == "first":
        assert expect >= 1
        out = b.replace(old.encode(), new.encode(), 1)
    else:
        out = b.replace(old.encode(), new.encode())
    p.write_bytes(out)
    return b


def restore(sb: pathlib.Path, rel: str, original: bytes) -> None:
    p = sb / rel
    p.write_bytes(original)
    assert p.read_bytes() == original, f"{rel}: restore failed"


def test_real_repo_is_reconciled() -> None:
    """The guard passes on the real repo, and says what it looked at.

    The reported counts are pinned on purpose: adding a workflow must move this
    number in the same change, which is exactly how the corpus pin in
    `test_check_tasks_refs.py` already behaves.
    """
    r = subprocess.run([PY, str(GUARD)], capture_output=True, cwd=str(REPO))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, f"guard failed on the real repo:\n{out}"
    n = len(list((REPO / WORKFLOWS).glob("*.yml")))
    assert f"{n} workflow(s) reconciled in 2 file(s)" in out, (
        f"the guard must report how much it reconciled; got:\n{out}"
    )


def test_every_judgment_is_pinned_by_a_mutation(tmp_path) -> None:
    sb = build_sandbox(tmp_path)
    rc, out = run(sb)
    assert rc == 0, f"sandbox baseline must be green:\n{out}"

    dash = f"{WORKFLOWS}/ci-dashboard.yml"
    trellis = f"{WORKFLOWS}/ci-trellis.yml"
    journal = f"{WORKFLOWS}/ci-journal.yml"

    # (label, judgment it pins, file, old, new, expected occurrences, how)
    cases = [
        (
            "A a row grows a path the YAML lacks",
            "fabrication", README,
            "| **Dashboard CI** (`ci-dashboard.yml`) | `dashboard/**` |",
            "| **Dashboard CI** (`ci-dashboard.yml`) | `dashboard/**`, `newfake/**` |",
            1, "first",
        ),
        (
            "B a row drops a path the YAML has",
            "omission", README,
            "`Cargo.toml`, `Cargo.lock`, `docker/docker-compose.yml`",
            "`Cargo.toml`, `docker/docker-compose.yml`",
            1, "first",
        ),
        (
            "C the table names a workflow that does not exist",
            "existence", README,
            "(`ci-journal.yml`)", "(`ci-journalX.yml`)",
            1, "first",
        ),
        (
            "D the YAML drifts without the README (the real scenario)",
            "omission", dash,
            ITEM.format(path="dashboard/**"),
            ITEM.format(path="dashboard/**") + '\n      - "newdir/**"',
            2, "all",
        ),
        (
            "E push changes without pull_request",
            "symmetry", trellis,
            ITEM.format(path=".trellis/scripts/**"),
            ITEM.format(path=".trellis/scriptsX/**"),
            2, "first",
        ),
        (
            "F a workflow stops targeting main",
            "branches", journal,
            "branches: [main]", "branches: [develop]",
            2, "all",
        ),
        (
            "G a workflow loses its paths filter",
            "filter-shape", dash,
            '    paths:\n' + ITEM.format(path="dashboard/**") + '\n'
            + ITEM.format(path=".github/workflows/ci-dashboard.yml") + '\n',
            "",
            2, "all",
        ),
        (
            "H the CI heading is renamed away",
            "non-vacuity", README,
            "\n## CI\n", "\n## Continuous Integration\n",
            1, "first",
        ),
        (
            "I the EN prose count drifts off the real number",
            "prose-count", README,
            # NOTE: these anchors quote the README's real wording, so adding
            # a workflow (which moves Nine -> Ten) breaks them. The anchor
            # count assertion is what makes that a loud failure at the anchor
            # rather than a mutation that quietly replaces nothing.
            "Ten independent workflows", "Nine independent workflows",
            1, "first",
        ),
        (
            "J the ZH prose count drifts off the real number",
            "prose-count", README_ZH,
            "\u5341\u5957\u72ec\u7acb\u5de5\u4f5c\u6d41",
            "\u4e5d\u5957\u72ec\u7acb\u5de5\u4f5c\u6d41",
            1, "first",
        ),
        (
            "M the prose count is reworded past the parser",
            "prose-count-missing", README,
            "Ten independent workflows", "Several independent workflows",
            1, "first",
        ),
        (
            "K a workflow drops its own YAML from its paths",
            "self-reference", f"{WORKFLOWS}/ci-dashboard.yml",
            ITEM.format(path=f"{WORKFLOWS}/ci-dashboard.yml"), "",
            2, "all",
        ),
        (
            "L a workflow loses workflow_dispatch",
            "manual-dispatch", f"{WORKFLOWS}/ci-journal.yml",
            "  workflow_dispatch:\n", "",
            1, "first",
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

    missing = sorted(set(JUDGMENTS) - set(covered))
    assert not missing, f"judgments no mutation reaches: {missing}"


def _load_guard():
    """Import the guard as a module so its parsers can be pinned directly."""
    spec = importlib.util.spec_from_file_location("check_readme_ci_table", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parsers_are_pinned_both_ways() -> None:
    """Judgments 8 and 9 rest on hand-written parsers, so pin both directions.

    A matcher that always returns True makes judgment 9 permanently green, and a
    count parser that guesses makes judgment 8 meaningless. Positive controls alone
    cannot tell those apart (T33 / #683: a positive control needs its own ablation),
    so every row below has a negative twin.

    The case-folding rows exist because the first draft looked the English word up
    without folding it, and "Nine" (sentence-initial) found nothing in a table keyed
    in lower case -- the guard reddened on the real repo, which is how it was found.
    """
    guard = _load_guard()

    covers = [
        (".github/workflows/ci-dashboard.yml",
         ".github/workflows/ci-dashboard.yml", True),
        (".github/workflows/**", ".github/workflows/ci-readme-ci-table.yml", True),
        ("crates/**", "crates/uc-python/src/lib.rs", True),
        ("docs/agents/*.md", "docs/agents/domain.md", True),
        ("crates/**", "cratesfoo/x", False),
        ("dashboard/**", "docs/dashboard/x", False),
        ("docs/agents/*.md", "docs/agents/nested/domain.md", False),
        (".github/workflows/ci-dashboard.yml",
         ".github/workflows/ci-journal.yml", False),
    ]
    for pattern, path, expected in covers:
        assert guard.pattern_covers(pattern, path) is expected, (
            f"pattern_covers({pattern!r}, {path!r}) should be {expected}")

    counts = [
        ("Nine independent workflows run on pushes", "README.md", (9, "Nine")),
        ("nine independent workflows", "README.md", (9, "nine")),
        ("9 independent workflows", "README.md", (9, "9")),
        ("\u4e5d\u5957\u72ec\u7acb\u5de5\u4f5c\u6d41", "README.zh-CN.md", (9, "\u4e5d")),
        ("Several independent workflows", "README.md", None),
        ("Nine workflows", "README.md", None),
        ("twenty independent workflows", "README.md", None),
    ]
    for text, name, expected in counts:
        got = guard.workflow_count(text, name)
        assert got == expected, f"workflow_count({text!r}) = {got}, want {expected}"


def test_mutations_are_independent(tmp_path) -> None:
    """Restoring really restores -- otherwise case N+1 is testing mutation N."""
    sb = build_sandbox(tmp_path)
    assert run(sb)[0] == 0
    p = sb / README
    original = p.read_bytes()
    mutate(sb, README, "(`ci-journal.yml`)", "(`ci-journalX.yml`)", 1, "first")
    assert run(sb)[0] != 0
    restore(sb, README, original)
    rc_after, out = run(sb)
    assert rc_after == 0, f"restore did not bring the sandbox back to green:\n{out}"
    assert p.read_bytes() == original


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
