"""Subprocess calls must be bounded (T50).

A `subprocess.run(...)` without `timeout=` waits forever: one hung git or nvidia-smi
occupies a worker slot with nothing to observe or cancel. The guard flags that shape and
exempts only the two child runners, whose parent owns the bound.

The tests prove the guard can say **no**, honours its exemption table, and that the real
repository is clean.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "check-subprocess-timeouts.py"
TARGET = REPO / "python" / "ultimate_coders"


def _load_guard():
    spec = importlib.util.spec_from_file_location("subprocess_timeout_guard", GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scan(tmp_path: pathlib.Path, source: str) -> list[tuple[int, str, str]]:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return _load_guard().scan(path)


def test_a_bounded_run_is_clean(tmp_path):
    findings = _scan(
        tmp_path,
        "import subprocess\n"
        "def run_bench():\n"
        "    return subprocess.run(['git', 'status'], timeout=30)\n",
    )
    assert findings == []


def test_an_unbounded_run_is_reported_with_its_function(tmp_path):
    findings = _scan(
        tmp_path,
        "import subprocess\n"
        "def run_bench():\n"
        "    return subprocess.run(['git', 'status'])\n",
    )
    assert findings == [(3, "run_bench", "run")]


def test_an_unbounded_check_output_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "import subprocess\n"
        "def probe():\n"
        "    return subprocess.check_output(['nvidia-smi'])\n",
    )
    assert findings == [(3, "probe", "check_output")]


def test_an_unbounded_popen_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "import subprocess\n"
        "def start():\n"
        "    return subprocess.Popen(['git', 'fetch'])\n",
    )
    assert findings == [(3, "start", "Popen")]


def test_module_level_calls_are_reported_as_module(tmp_path):
    findings = _scan(
        tmp_path,
        "import subprocess\n"
        "subprocess.call(['git', 'status'])\n",
    )
    assert findings == [(2, "<module>", "call")]


def test_other_objects_named_run_are_not_subprocess(tmp_path):
    findings = _scan(
        tmp_path,
        "class Runner:\n"
        "    def run(self):\n"
        "        return runner.run(['git'])\n"
        "runner = Runner()\n",
    )
    assert findings == []


def test_a_parsed_error_is_reported_not_swallowed(tmp_path):
    findings = _scan(tmp_path, "def broken(:\n    pass\n")
    assert findings and findings[0][2].startswith("could not parse")


def test_the_exemption_table_documents_its_sites():
    exempt = _load_guard().EXEMPT
    assert len(exempt) == 2, "an exemption is a claim; keep the list exactly as reviewed"
    for key, reason in exempt.items():
        path, function = key
        assert (REPO / path).is_file(), f"exemption names a missing file: {path}"
        assert function
        assert len(reason) > 20, f"exemption {key} needs a real reason, got {reason!r}"


def test_the_real_repository_is_clean():
    # The interpreter running this test, not a platform-specific venv path.
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    assert "subprocess timeout check passed." in result.stdout


def test_the_guard_actually_walks_the_package():
    modules = sorted(TARGET.rglob("*.py"))
    assert len(modules) >= 100, f"only {len(modules)} module(s) found under python/ultimate_coders"
