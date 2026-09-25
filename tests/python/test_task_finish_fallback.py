"""`task.py finish` must not print a success it did not perform (#679).

Measured 2026-09-17::

    $ python .trellis/scripts/task.py finish
    ✓ Cleared current task (was: .trellis/tasks/09-13-t3-graph-runtime)
    Source: session-fallback:qoder_64c9007e-...
    $ python .trellis/scripts/task.py current
    .trellis/tasks/09-13-t3-graph-runtime        # unchanged

`clear_active_task` deletes `_context_path(context_key)` -- the CURRENT
session's file -- while the value it reports comes from `resolve_active_task`,
which falls back to scanning `.runtime/sessions/*.json` when the current
session has no file of its own. Reported and deleted were different objects, so
success was printed for a deletion that never happened.

Three properties of the target shape this harness:

* the CLI runs as a subprocess against a SYNTHETIC repo, so the real
  `.trellis/tasks/` tree is never mutated. A test that called `task.py create`
  would leave a task directory (plus `.trellis/.runtime/` state) behind on
  every run, permanently dirtying the working tree;
* `get_repo_root()` walks up from cwd looking for `.trellis` and
  `resolve_context_key()` honours `TRELLIS_CONTEXT_ID`, so a temp directory
  plus one environment variable reproduce the measured state exactly;
* the cross-file deletion pinned here is bounded by the fallback itself:
  `_resolve_single_session_fallback` fires only with EXACTLY ONE session file,
  and the two-window case below asserts it still refuses to guess.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TASK_PY = REPO_ROOT / ".trellis" / "scripts" / "task.py"

TASK_REF = ".trellis/tasks/09-13-t3-graph-runtime"
MY_KEY = "codebuddy_this_shell"
OTHER_KEY = "qoder_64c9007e"


def _write_session(root, key, task_ref):
    """Write one session pointer.

    Bytes, not text: `Path.write_text(newline=...)` needs Python 3.10 while the
    CI matrix still includes 3.9, and the default would translate newlines.
    """
    path = root / ".trellis" / ".runtime" / "sessions" / (key + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"platform": key.split("_")[0], "current_task": task_ref}
    path.write_bytes(json.dumps(payload).encode("utf-8"))
    return path


def _run(root, *argv, context_key=MY_KEY):
    """Run the real CLI against a synthetic repo.

    `TRELLIS_CONTEXT_ID` is checked first by `resolve_context_key()`, so setting
    it is enough to pin which window this shell is -- and overriding it stops an
    ambient value in the developer's environment from changing the scenario.
    """
    env = dict(os.environ)
    env["TRELLIS_CONTEXT_ID"] = context_key
    return subprocess.run(
        [sys.executable, str(TASK_PY)] + list(argv),
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_finish_clears_a_fallback_sourced_pointer(tmp_path):
    """The measured repro: this shell has no file of its own, so the reported
    value lives in another window's file. That file is the one to clear."""
    other = _write_session(tmp_path, OTHER_KEY, TASK_REF)

    result = _run(tmp_path, "finish")

    assert result.returncode == 0, result.stderr
    assert "✓" in result.stdout
    assert not other.exists(), "the pointer that was reported is still on disk"

    after = _run(tmp_path, "current")
    assert after.returncode == 1
    assert after.stdout.strip() == ""


def test_finish_clears_the_current_session_file(tmp_path):
    """Control: the ordinary single-window case must keep working."""
    mine = _write_session(tmp_path, MY_KEY, TASK_REF)

    result = _run(tmp_path, "finish")

    assert result.returncode == 0, result.stderr
    assert "✓" in result.stdout
    assert not mine.exists()

    after = _run(tmp_path, "current")
    assert after.returncode == 1
    assert after.stdout.strip() == ""


def test_finish_refuses_to_cross_delete_a_second_window(tmp_path):
    """Two windows: my file goes, the other stays, and no ✓ is printed while the
    task is still resolvable -- the fallback refuses to guess here, and `finish`
    must not turn that refusal into a false success."""
    mine = _write_session(tmp_path, MY_KEY, TASK_REF)
    other = _write_session(tmp_path, OTHER_KEY, TASK_REF)

    result = _run(tmp_path, "finish")

    assert not mine.exists()
    assert other.exists(), "cleared a pointer belonging to another window"
    assert "✓" not in result.stdout
    assert result.returncode != 0
