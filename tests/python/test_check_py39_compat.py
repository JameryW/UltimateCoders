"""Python 3.9 compatibility guard.

CI runs every Python leg on 3.9 and 3.12, but development happens on a newer interpreter,
so a 3.10+ construct passes locally and fails only the 3.9 leg — which is how
`zip(..., strict=True)` reached `main`. The guard flags constructs whose 3.9 behaviour is a
hard failure, names the version that introduced each one, and never reports annotations
(`from __future__ import annotations` makes `A | B` annotations safe).

The tests prove the guard can say **no** for each rule, that guarded imports stay quiet,
and that the real repository is clean.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "check-py39-compat.py"
TARGETS = (
    REPO / "python" / "ultimate_coders",
    REPO / "tests" / "python",
    REPO / "scripts",
)


def _load_guard():
    spec = importlib.util.spec_from_file_location("py39_compat_guard", GUARD)
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


def test_plain_python_39_code_is_clean(tmp_path):
    findings = _scan(
        tmp_path,
        "from __future__ import annotations\n"
        "def combine(left: list[int], right: list[int]) -> list[int]:\n"
        "    return [a + b for a, b in zip(left, right)]\n",
    )
    assert findings == []


def test_annotations_may_use_pep604_unions(tmp_path):
    # `str | None` as an ANNOTATION is safe on 3.9 with the future import: it is never
    # evaluated. Only runtime uses (isinstance/issubclass) are a failure.
    findings = _scan(
        tmp_path,
        "from __future__ import annotations\n"
        "def parse(value: str | None = None) -> int | None:\n"
        "    return None\n",
    )
    assert findings == []


def test_zip_with_strict_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "def pair(left, right):\n    return list(zip(left, right, strict=True))\n",
    )
    assert _rules(findings) == ["zip-strict"]
    assert findings[0][0] == 2
    assert "3.10" in findings[0][2]


def test_pep604_in_isinstance_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "def check(value):\n    return isinstance(value, int | str)\n",
    )
    assert _rules(findings) == ["pep604-runtime-union"]


def test_pep604_in_issubclass_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "def check(cls):\n    return issubclass(cls, int | str)\n",
    )
    assert _rules(findings) == ["pep604-runtime-union"]


def test_bit_count_is_reported(tmp_path):
    findings = _scan(tmp_path, "def popcount(value):\n    return value.bit_count()\n")
    assert _rules(findings) == ["api:int.bit_count"]


def test_newer_stdlib_attribute_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "import itertools\ndef pairs(items):\n    return list(itertools.pairwise(items))\n",
    )
    assert _rules(findings) == ["api:itertools.pairwise"]


def test_datetime_utc_is_reported(tmp_path):
    findings = _scan(tmp_path, "import datetime\nNOW = datetime.UTC\n")
    assert _rules(findings) == ["api:datetime.UTC"]


def test_new_typing_member_is_reported(tmp_path):
    findings = _scan(tmp_path, "from typing import Self, TypeAlias\n")
    assert _rules(findings) == ["typing:Self", "typing:TypeAlias"]
    assert {line for line, _, _ in findings} == {1}


def test_new_enum_member_is_reported(tmp_path):
    findings = _scan(tmp_path, "from enum import StrEnum\n")
    assert _rules(findings) == ["enum:StrEnum"]


def test_bare_tomllib_import_is_reported(tmp_path):
    findings = _scan(tmp_path, "import tomllib\ntomllib.loads('')\n")
    assert _rules(findings) == ["stdlib:tomllib"]
    assert "3.11" in _details(findings)


def test_guarded_tomllib_import_is_clean(tmp_path):
    findings = _scan(
        tmp_path,
        "try:\n"
        "    import tomllib\n"
        "except ImportError:  # pragma: no cover - Python < 3.11\n"
        "    tomllib = None\n",
    )
    assert findings == []


def test_tomllib_guarded_from_import_is_clean(tmp_path):
    findings = _scan(
        tmp_path,
        "try:\n    from tomllib import loads\nexcept ModuleNotFoundError:\n    loads = None\n",
    )
    assert findings == []


def test_tomllib_under_a_narrow_handler_is_reported(tmp_path):
    # A handler that does not catch an import failure is not a guard.
    findings = _scan(
        tmp_path,
        "try:\n    import tomllib\nexcept ValueError:\n    tomllib = None\n",
    )
    assert _rules(findings) == ["stdlib:tomllib"]


def test_a_310_only_statement_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "def route(kind):\n"
        "    match kind:\n"
        "        case 'a':\n"
        "            return 1\n"
        "    return 0\n",
    )
    # On 3.9 the parser refuses first, so the guard reports the syntax itself; on 3.10+
    # the AST carries the statement and the guard names the construct.
    assert len(findings) == 1
    assert findings[0][1] in {"unparseable", "match-statement"}


def test_an_except_star_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "def run():\n    try:\n        pass\n    except* ValueError:\n        pass\n",
    )
    assert len(findings) == 1
    assert findings[0][1] in {"unparseable", "except-star"}


def test_a_syntax_error_is_reported_not_swallowed(tmp_path):
    findings = _scan(tmp_path, "def broken(:\n    pass\n")
    assert findings and findings[0][1] == "unparseable"


def test_findings_are_sorted_by_line(tmp_path):
    findings = _scan(
        tmp_path,
        "from typing import Self\ndef a(value):\n    return value.bit_count()\n",
    )
    assert [line for line, _, _ in findings] == [1, 3]


def test_the_exemption_table_documents_its_sites():
    exempt = _load_guard().EXEMPT
    for (path, rule), reason in exempt.items():
        assert (REPO / path).is_file(), f"exemption names a missing file: {path}"
        assert rule
        assert len(reason) > 20, f"exemption {(path, rule)} needs a real reason"


def test_the_guard_targets_every_first_party_python_tree():
    guard = _load_guard()
    assert {path.resolve() for path in guard.TARGETS} == {path.resolve() for path in TARGETS}


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
    assert "3.9 compatibility check passed." in result.stdout


def test_the_guard_actually_walks_the_trees():
    modules = [path for target in TARGETS for path in target.rglob("*.py")]
    assert len(modules) >= 400, f"only {len(modules)} file(s) found across the guard targets"
