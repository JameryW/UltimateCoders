"""Test temporary-directory guard.

A full-suite run once left two `tmp*` directories in the repository root: `tempfile.mkdtemp()`
plus a hand-written `finally:` cleanup only tears down when the test reaches its own end, so a
failing assertion leaks the directory. `git add -A` then staged the leftovers as embedded
repositories and the line-ending guard's gitlink count moved from 1 to 4.

The tests prove the guard can say **no** for both spellings of the call, that the pytest-owned
and context-managed alternatives stay quiet, that production code is out of scope, and that the
real test tree is clean.
"""

from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "check-test-tempdirs.py"
TARGETS = (REPO / "tests" / "python",)


def _load_guard():
    spec = importlib.util.spec_from_file_location("test_tempdirs_guard", GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scan(tmp_path: pathlib.Path, source: str) -> list[tuple[int, str, str]]:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return _load_guard().scan(path)


def _rules(findings: list[tuple[int, str, str]]) -> list[str]:
    return [rule for _, rule, _ in findings]


def _details(findings: list[tuple[int, str, str]]) -> str:
    return " | ".join(detail for _, _, detail in findings)


# ── the guard can say no ─────────────────────────────────────────


def test_module_attribute_call_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "import tempfile\ndef test_x():\n    root = tempfile.mkdtemp()\n    assert root\n",
    )
    assert _rules(findings) == ["tests:mkdtemp"]
    assert findings[0][0] == 3
    assert "tmp_path" in _details(findings)


def test_direct_import_call_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "from tempfile import mkdtemp\n"
        "def test_x():\n"
        "    root = mkdtemp(prefix='x-')\n"
        "    assert root\n",
    )
    assert _rules(findings) == ["tests:mkdtemp"]
    assert findings[0][0] == 3


def test_aliased_imports_are_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "import tempfile as tf\n"
        "from tempfile import mkdtemp as make_dir\n"
        "def test_x():\n"
        "    first = tf.mkdtemp()\n"
        "    second = make_dir()\n"
        "    assert first and second\n",
    )
    assert _rules(findings) == ["tests:mkdtemp", "tests:mkdtemp"]
    assert [line for line, _, _ in findings] == [4, 5]


def test_dir_argument_does_not_excuse_the_leak(tmp_path):
    findings = _scan(
        tmp_path,
        "import tempfile, pathlib\n"
        "def test_x(tmp_path):\n"
        "    root = tempfile.mkdtemp(dir=tmp_path)\n"
        "    assert root\n",
    )
    assert _rules(findings) == ["tests:mkdtemp"]


# ── the pytest-owned alternatives stay quiet ─────────────────────


def test_tmp_path_fixture_is_clean(tmp_path):
    findings = _scan(
        tmp_path,
        "def test_x(tmp_path):\n"
        "    (tmp_path / '.codegraph').mkdir()\n"
        "    assert tmp_path.is_dir()\n",
    )
    assert findings == []


def test_temporary_directory_is_clean(tmp_path):
    findings = _scan(
        tmp_path,
        "import tempfile\n"
        "def test_x():\n"
        "    with tempfile.TemporaryDirectory() as root:\n"
        "        assert root\n",
    )
    assert findings == []


def test_unrelated_mkdtemp_attribute_is_clean(tmp_path):
    findings = _scan(
        tmp_path,
        "import shutil\ndef test_x(builder):\n    root = builder.mkdtemp()\n    assert root\n",
    )
    assert findings == []


def test_mention_in_a_docstring_is_clean(tmp_path):
    findings = _scan(
        tmp_path,
        '"""Never call mkdtemp() or tempfile.mkdtemp() in tests."""\n'
        "def test_x(tmp_path):\n"
        "    assert tmp_path\n",
    )
    assert findings == []


def test_unparseable_file_is_left_to_other_guards(tmp_path):
    findings = _scan(tmp_path, "def broken(:\n")
    assert findings == []


# ── main() reports and exits ─────────────────────────────────────


def _targets_relative() -> list[str]:
    guard = _load_guard()
    return [path.relative_to(REPO).as_posix() for path in guard.TARGETS]


def test_targets_are_pinned():
    assert _targets_relative() == ["tests/python"]


def test_exemptions_are_empty_today():
    assert _load_guard().EXEMPT == {}


def test_real_test_tree_is_clean():
    guard = _load_guard()
    problems: list[str] = []
    for target in guard.TARGETS:
        for path in sorted(target.rglob("*.py")):
            relative = path.relative_to(REPO).as_posix()
            for line, rule, detail in guard.scan(path):
                if (relative, rule) in guard.EXEMPT:
                    continue
                problems.append(f"{relative}:{line}: {detail}")
    assert problems == []


def test_main_exits_zero_on_the_real_repo(capsys):
    assert _load_guard().main() == 0
    out = capsys.readouterr().out
    assert "test temp dir check passed." in out
    assert "documented exemption(s)" in out
