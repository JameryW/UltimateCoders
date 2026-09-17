"""`task.py archive` must repoint the task's own citations at its new location.

Measured 2026-09-17 (#678): `archive_task_dir` moves a task directory with
`shutil.move` and nothing else. Every citation the task makes to its OWN files is
a pre-archive path -- `implement.jsonl` / `check.jsonl` name
`.trellis/tasks/<name>/prd.md`, and `task.json` names its own
`research/notes.md` -- so the move breaks all of them at once, silently. That is
the dominant shape of the dangling `.trellis/tasks/**` corpus, and the move is
what creates every instance of it.

The repair covers exactly what the framework itself writes: JSON carriers
(`.jsonl`, `.json`), where a `.trellis/...` string is a machine-typed citation.
Prose in `.md` is deliberately NOT rewritten -- a path in prose can be a
deliberate historical note ("this task moved from X"), and this repository's
rule for its other reference guard is already that a mention is not a reference.
The boundary is asserted here rather than left implied.

Harness shape: a synthetic repo in `tmp_path`, the real CLI as a subprocess with
`--no-commit` (so no git is touched), and the archive month computed the same way
the CLI computes it. The real `.trellis/tasks/` tree is never mutated.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TASK_PY = REPO_ROOT / ".trellis" / "scripts" / "task.py"

NAME = "01-01-demo"


def _month() -> str:
    return datetime.datetime.now().strftime("%Y-%m")


def _task_dir(root, name=NAME):
    return root / ".trellis" / "tasks" / name


def _make_task(root, name=NAME, *, jsonl_refs=True, md_ref=True):
    """Build a minimal but realistic task directory."""
    task = _task_dir(root, name)
    (task / "research").mkdir(parents=True)
    (task / "prd.md").write_bytes(
        (
            f"# {name}\n\nContext: `.trellis/tasks/{name}/research/notes.md`\n"
            if md_ref
            else f"# {name}\n"
        ).encode("utf-8")
    )
    (task / "research" / "notes.md").write_bytes(b"# notes\n")
    (task / "task.json").write_bytes(
        json.dumps(
            {
                "status": "in_progress",
                "completedAt": None,
                "research": [f".trellis/tasks/{name}/research/notes.md"],
            }
        ).encode("utf-8")
    )
    if jsonl_refs:
        lines = [
            {"file": f".trellis/tasks/{name}/prd.md", "reason": "requirements"},
            {"file": f".trellis/tasks/{name}/research/notes.md", "reason": "notes"},
        ]
        (task / "implement.jsonl").write_bytes(
            ("\n".join(json.dumps(x) for x in lines) + "\n").encode("utf-8")
        )
        (task / "check.jsonl").write_bytes(
            (json.dumps({"file": f".trellis/tasks/{name}/prd.md", "reason": "ac"}) + "\n").encode(
                "utf-8"
            )
        )
    return task


def _run(root, *argv):
    return subprocess.run(
        [sys.executable, str(TASK_PY)] + list(argv),
        cwd=str(root),
        env=dict(os.environ),
        capture_output=True,
        text=True,
    )


def _archive(root, name=NAME):
    return _run(root, "archive", name, "--no-commit")


def _dest(root, name=NAME):
    return root / ".trellis" / "tasks" / "archive" / _month() / name


def test_archive_repoints_own_json_citations(tmp_path):
    """The measured shape: the moved task cites its own files, JSON carriers."""
    _make_task(tmp_path)
    result = _archive(tmp_path)
    assert result.returncode == 0, result.stderr
    dest = _dest(tmp_path)
    assert dest.is_dir(), "task was not archived"

    implement = (dest / "implement.jsonl").read_text(encoding="utf-8")
    assert f".trellis/tasks/{NAME}/" not in implement, "pre-archive path survived"
    # every citation in the file must now resolve, from the repo root
    for line in implement.splitlines():
        assert (tmp_path / json.loads(line)["file"]).is_file()

    check = (dest / "check.jsonl").read_text(encoding="utf-8")
    assert f".trellis/tasks/archive/{_month()}/{NAME}/prd.md" in check

    task_json = json.loads((dest / "task.json").read_text(encoding="utf-8"))
    assert task_json["research"] == [
        f".trellis/tasks/archive/{_month()}/{NAME}/research/notes.md"
    ]
    assert (tmp_path / task_json["research"][0]).is_file()


def test_archive_leaves_prose_mentions_alone(tmp_path):
    """The boundary: a path inside `.md` prose may be a deliberate historical
    note, so the automated repair does not touch it. Asserted, not implied."""
    _make_task(tmp_path)
    assert _archive(tmp_path).returncode == 0
    prd = (_dest(tmp_path) / "prd.md").read_text(encoding="utf-8")
    assert f".trellis/tasks/{NAME}/research/notes.md" in prd


def test_archive_does_not_touch_another_tasks_citations(tmp_path):
    """Scope safety: only citations OF the moved task are repointed. Another
    task's dangling citation to it is reported, never rewritten."""
    _make_task(tmp_path)
    other = _make_task(tmp_path, name="01-02-other", jsonl_refs=False, md_ref=False)
    (other / "implement.jsonl").write_bytes(
        (
            json.dumps({"file": f".trellis/tasks/{NAME}/prd.md", "reason": "upstream"})
            + "\n"
        ).encode("utf-8")
    )

    result = _archive(tmp_path)
    assert result.returncode == 0, result.stderr

    text = (other / "implement.jsonl").read_text(encoding="utf-8")
    assert f".trellis/tasks/{NAME}/prd.md" in text, "another task's file was rewritten"
    # ...and the breakage is reported rather than left silent
    assert ".trellis/tasks/01-02-other/implement.jsonl" in result.stderr
    assert ".trellis/tasks/" + NAME in result.stderr


def _load_task_utils():
    """Import the framework module under test (package-relative imports mean the
    scripts directory has to be on sys.path)."""
    scripts_dir = REPO_ROOT / ".trellis" / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import common.task_utils as task_utils  # noqa: E402

    return task_utils


def test_rewrite_is_idempotent(tmp_path):
    """A second pass must report nothing and change nothing.

    Re-running the CLI cannot test this: the task is no longer at the
    pre-archive path, so the archive command never reaches the repair. The
    function is therefore called directly.
    """
    _make_task(tmp_path)
    assert _archive(tmp_path).returncode == 0
    dest = _dest(tmp_path)
    task_utils = _load_task_utils()
    before = {p.name: p.read_bytes() for p in sorted(dest.rglob("*")) if p.is_file()}

    second = task_utils.rewrite_archived_task_refs(dest, tmp_path)

    assert second == {}, f"second pass rewrote {second}"
    after = {p.name: p.read_bytes() for p in sorted(dest.rglob("*")) if p.is_file()}
    assert after == before, "idempotent pass mutated content"


def test_archive_does_not_rewrite_a_longer_task_name(tmp_path):
    """A task name can be a proper PREFIX of another task name -- this repository
    has 16 such pairs (06-15-tui < 06-15-tui-unit-tests, 06-29-worker <
    06-29-worker-omp, ...).

    So the matcher has to be boundary-aware: archiving 01-01-demo must not
    repoint a citation of 01-01-demo-extended, which is a different task and may
    live in a different archive month. A plain substring replace would do it
    silently, and the result would still look like a valid path.
    """
    longer = NAME + "-extended"
    _make_task(tmp_path, name=longer, jsonl_refs=False, md_ref=False)
    task = _make_task(tmp_path)
    (task / "implement.jsonl").write_bytes(
        (
            json.dumps({"file": f".trellis/tasks/{NAME}/prd.md", "reason": "own"})
            + "\n"
            + json.dumps({"file": f".trellis/tasks/{longer}/prd.md", "reason": "peer"})
            + "\n"
        ).encode("utf-8")
    )

    result = _archive(tmp_path)
    assert result.returncode == 0, result.stderr

    lines = [
        json.loads(line)
        for line in (_dest(tmp_path) / "implement.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    by_reason = {item["reason"]: item["file"] for item in lines}
    assert by_reason["own"] == f".trellis/tasks/archive/{_month()}/{NAME}/prd.md"
    assert by_reason["peer"] == f".trellis/tasks/{longer}/prd.md", (
        "a longer task name sharing a prefix was rewritten by accident"
    )
    # the peer citation must still resolve -- it points at a live task
    assert (tmp_path / by_reason["peer"]).is_file()
