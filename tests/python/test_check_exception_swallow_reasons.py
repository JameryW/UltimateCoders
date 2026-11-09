"""Broad silent handlers need a written reason (T51).

`except Exception: pass` erases the only evidence something went wrong. The rule is not
"never swallow" -- most such handlers here are legitimate best-effort paths -- but
"swallow out loud", with a `# best-effort: <reason>` marker that a reader and a reviewer
can see. Narrow catches are out of scope: the exception type is the explanation.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "check-exception-swallow-reasons.py"
TARGET = REPO / "python" / "ultimate_coders"


def _load_guard():
    spec = importlib.util.spec_from_file_location("exception_swallow_guard", GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scan(tmp_path: pathlib.Path, source: str) -> list[tuple[int, str]]:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return _load_guard().scan(path)


def test_an_unexplained_broad_swallow_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        pass\n",
    )
    assert findings == [(4, "Exception")]


def test_a_marker_on_the_handler_line_is_enough(tmp_path):
    findings = _scan(
        tmp_path,
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:  # best-effort: metrics must never break the request\n"
        "        pass\n",
    )
    assert findings == []


def test_a_marker_just_above_the_try_block_also_counts(tmp_path):
    findings = _scan(
        tmp_path,
        "def f():\n"
        "    # best-effort: cleanup during an error path\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        pass\n",
    )
    assert findings == []


def test_a_bare_except_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except:\n"
        "        pass\n",
    )
    assert findings == [(4, "bare except")]


def test_a_narrow_catch_needs_no_reason(tmp_path):
    findings = _scan(
        tmp_path,
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except FileNotFoundError:\n"
        "        pass\n",
    )
    assert findings == []


def test_a_handler_that_does_more_than_pass_is_out_of_scope(tmp_path):
    findings = _scan(
        tmp_path,
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        log.warning('failed', exc_info=True)\n",
    )
    assert findings == [], "logging (or returning a value) is already visible behaviour"


def test_the_real_repository_is_clean():
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    assert "exception swallow check passed." in result.stdout


def test_the_guard_actually_walks_the_package():
    modules = sorted(TARGET.rglob("*.py"))
    assert len(modules) >= 100, f"only {len(modules)} module(s) under python/ultimate_coders"
