"""Static AST survey: determinism, tolerance and bounds.

The survey is a read-only observation used by knowledge pages, so these tests
pin its two contracts: the same tree always produces the same survey/digest, and
nothing in the tree (syntax errors, tooling directories, huge inputs) can make
the survey raise.
"""

from __future__ import annotations

import textwrap

from ultimate_coders.knowledge.survey import (
    ModuleSurvey,
    SurveyReport,
    SurveyStats,
    SymbolInfo,
    repo_survey,
    summarize_survey,
    survey_digest,
    survey_report,
    survey_source,
)

SOURCE = textwrap.dedent(
    '''\
    """Demo module."""
    import os
    from collections import OrderedDict

    CONSTANT = 1

    class Widget:
        """A widget."""

        def render(self):
            def helper():
                return 1

            return helper()

    async def main():
        return 1
    '''
)


def test_survey_source_extracts_symbols_imports_and_docstring() -> None:
    survey = survey_source("pkg/mod.py", SOURCE)

    assert survey is not None
    assert survey.path == "pkg/mod.py"
    assert survey.module == "pkg.mod"
    assert survey.docstring == "Demo module."
    assert survey.imports == ("collections.OrderedDict", "os")

    by_qualname = {symbol.qualname: symbol for symbol in survey.symbols}
    assert set(by_qualname) == {"Widget", "Widget.render", "Widget.render.helper", "main"}
    assert by_qualname["Widget"].kind == "class"
    assert by_qualname["Widget"].start_line == 7
    assert by_qualname["Widget.render"].kind == "method"
    assert by_qualname["Widget.render"].parent == "Widget"
    assert by_qualname["Widget.render.helper"].kind == "function"
    assert by_qualname["Widget.render.helper"].parent == "Widget.render"
    assert by_qualname["main"].kind == "function"
    assert by_qualname["main"].start_line == 16


def test_survey_source_orders_symbols_by_source_position() -> None:
    survey = survey_source("m.py", SOURCE)

    assert survey is not None
    positions = [(symbol.start_line, symbol.end_line) for symbol in survey.symbols]
    assert positions == sorted(positions)
    for symbol in survey.symbols:
        assert symbol.end_line >= symbol.start_line
        assert symbol.path == "m.py"


def test_survey_source_handles_relative_and_star_imports() -> None:
    source = "from . import sibling\nfrom .pkg import item\nfrom .. import parent\nimport json\n"

    survey = survey_source("m.py", source)

    assert survey is not None
    assert survey.imports == ("..parent", ".pkg.item", ".sibling", "json")


def test_survey_source_returns_none_for_unparseable_input() -> None:
    assert survey_source("x.py", "def broken(:\n") is None
    assert survey_source("x.py", "") is None
    assert survey_source("x.py", "   \n") is None
    assert survey_source("x.py", None) is None
    assert survey_source("x.py", b"print(1)") is None


def test_survey_source_bounds_symbols_and_docstring() -> None:
    long_doc = "word " * 200
    source = f'"""{long_doc}"""\ndef a(): pass\ndef b(): pass\ndef c(): pass\n'

    survey = survey_source("m.py", source, max_symbols_per_file=2)

    assert survey is not None
    assert [symbol.name for symbol in survey.symbols] == ["a", "b"]
    assert len(survey.docstring) <= 300
    assert survey.docstring.endswith("...")

    empty = survey_source("m.py", "def a(): pass\n", max_symbols_per_file=0)
    assert empty is not None
    assert empty.symbols == ()


def test_survey_source_derives_module_name_from_path() -> None:
    package = survey_source("pkg/__init__.py", "x = 1\n")
    top_level = survey_source("top.py", "x = 1\n")
    explicit = survey_source("a/b.py", "x = 1\n", module="explicit.mod")

    assert package is not None and package.module == "pkg"
    assert top_level is not None and top_level.module == "top"
    assert explicit is not None and explicit.module == "explicit.mod"


def test_repo_survey_is_deterministic_and_skips_noise(tmp_path) -> None:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    for noise in (".venv", "node_modules", "vendor", "build", ".hidden"):
        (root / noise).mkdir()
        (root / noise / "noise.py").write_text("def noise():\n    pass\n", encoding="utf-8")

    (root / "__init__.py").write_text("# package\n", encoding="utf-8")
    (root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (root / "z.py").write_text("def z():\n    return 2\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("class B:\n    pass\n", encoding="utf-8")
    (root / "tests" / "test_hidden.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (root / "test_root.py").write_text("def test_y():\n    pass\n", encoding="utf-8")
    (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    first = survey_report(root)

    assert [module.path for module in first.modules] == [
        "__init__.py",
        "a.py",
        "pkg/b.py",
        "z.py",
    ]
    assert first.stats.parsed_files == 4
    assert first.stats.scanned_files == 5
    assert first.stats.skipped_files == 1
    assert first.stats.skipped_dirs == 5
    assert all("noise" not in module.path for module in first.modules)

    second = survey_report(root)
    assert first == second
    assert survey_digest(first) == survey_digest(second)

    with_tests = survey_report(root, include_tests=True)
    paths = [module.path for module in with_tests.modules]
    assert "tests/test_hidden.py" in paths
    assert "test_root.py" in paths
    assert with_tests.stats.parsed_files == 6


def test_repo_survey_bounds_files_and_symbols(tmp_path) -> None:
    root = tmp_path / "bounded"
    root.mkdir()
    (root / "a.py").write_text("def a(): pass\ndef b(): pass\ndef c(): pass\n", encoding="utf-8")
    (root / "b.py").write_text("x = 1\n", encoding="utf-8")
    (root / "c.py").write_text("y = 2\n", encoding="utf-8")

    limited = survey_report(root, max_files=2)

    assert [module.path for module in limited.modules] == ["a.py", "b.py"]
    assert limited.stats.limited_files == 1

    truncated = survey_report(root, max_symbols_per_file=1)

    assert truncated.stats.truncated_files == 1
    assert [len(module.symbols) for module in truncated.modules] == [1, 0, 0]

    assert len(repo_survey(root)) == 3
    assert repo_survey(tmp_path / "missing") == ()
    assert survey_report(tmp_path / "missing").modules == ()


def test_survey_digest_is_stable_and_source_sensitive() -> None:
    one = survey_source("m.py", "def a():\n    pass\n")
    two = survey_source("m.py", "def b():\n    pass\n")

    assert survey_digest(one) == survey_digest(one)
    assert survey_digest(one) != survey_digest(two)
    assert survey_digest((one,)) == survey_digest(one)
    assert survey_digest(None) == survey_digest(())
    assert len(survey_digest(one)) == 64

    # Traversal counters describe the budget, not the observed tree.
    report = SurveyReport(
        modules=(one,),
        stats=SurveyStats(scanned_files=9, parsed_files=1, skipped_files=4),
    )
    plain = SurveyReport(modules=(one,))
    assert survey_digest(report) == survey_digest(plain)
    assert survey_digest(report) == survey_digest(report.modules)


def test_summarize_survey_is_log_safe() -> None:
    survey = survey_source("pkg/m.py", SOURCE)
    assert survey is not None
    report = SurveyReport(
        modules=(survey,),
        stats=SurveyStats(scanned_files=3, parsed_files=1, skipped_files=2),
    )

    summary = summarize_survey(report)

    assert summary == {"modules": 1, "symbols": 4, "files": 3, "skipped": 2}
    assert summarize_survey(None) == {"modules": 0, "symbols": 0, "files": 0, "skipped": 0}
    assert summarize_survey((survey,))["modules"] == 1
    assert summarize_survey({"modules": 2, "symbols": 7, "files": 2, "skipped": 0})["symbols"] == 7
    assert "Demo module." not in str(summary)
    assert "pkg/m.py" not in str(summary)


def test_summaries_never_echo_source_text() -> None:
    survey = survey_source("m.py", '"""Secret docstring."""\ndef f():\n    pass\n')

    assert survey is not None
    module_summary = survey.to_summary()
    assert "docstring" not in module_summary
    assert "Secret docstring." not in str(module_summary)
    assert module_summary["symbols"] == 1

    symbol_summary = survey.symbols[0].to_summary()
    assert set(symbol_summary) == {"path", "kind", "qualname", "start_line", "end_line"}
    assert survey.to_dict()["docstring"] == "Secret docstring."


def test_records_tolerate_malformed_payloads() -> None:
    symbol = SymbolInfo.from_dict(
        {"path": 123, "kind": "weird", "qualname": "K.f", "start_line": -5, "end_line": "x"}
    )

    assert symbol.kind == "function"
    assert symbol.path == "123"
    assert symbol.name == "f"
    assert symbol.parent == "K"
    assert symbol.start_line == 0
    assert symbol.end_line == 0

    module = ModuleSurvey.from_dict(
        {
            "path": "m.py",
            "symbols": ["junk", {"qualname": "ok"}],
            "imports": [None, "os", "os"],
            "docstring": None,
        }
    )
    assert module.path == "m.py"
    assert [item.qualname for item in module.symbols] == ["ok"]
    assert module.imports == ("os",)

    report = SurveyReport.from_dict(
        {
            "modules": [None, {"path": "x.py", "symbols": [{"qualname": "q"}]}],
            "stats": {"scanned_files": "nope"},
        }
    )
    assert [item.path for item in report.modules] == ["x.py"]
    assert report.stats.scanned_files == 0
