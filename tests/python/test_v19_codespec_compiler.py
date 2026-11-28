"""Tests for the V19 CodeSpec dual-spec compiler (#73)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.codespec_compiler import (
    CODESPEC_COMPILER_ENV,
    CodeSpecCompiler,
    ComponentEvidence,
    Requirement,
    codespec_compiler_enabled,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CODESPEC_COMPILER_ENV, raising=False)
    assert not codespec_compiler_enabled()

    monkeypatch.setenv(CODESPEC_COMPILER_ENV, "true")
    assert codespec_compiler_enabled()


def test_compile_builds_architecture_behavior_and_chains() -> None:
    spec = CodeSpecCompiler().compile(
        [
            Requirement("R1", "Cache", "cache invalidation is exact", ("invalidate",)),
            Requirement("R2", "API", "API returns fresh values", ("get",)),
        ],
        [
            ComponentEvidence(
                "Cache",
                "src/cache.py",
                symbols=("invalidate", "get"),
                dependencies=("API",),
            ),
            ComponentEvidence("API", "src/api.py", symbols=("get",)),
        ],
    )

    assert spec.complete
    assert spec.architecture.components == ("API", "Cache")
    assert spec.architecture.path_for("Cache") == "src/cache.py"
    assert spec.architecture.edges == (("Cache", "API"),)
    assert spec.behavior.clauses == (
        "API returns fresh values",
        "cache invalidation is exact",
    )
    assert [chain.requirement_id for chain in spec.chains] == ["R1", "R2"]


def test_missing_component_is_reported_as_gap() -> None:
    spec = CodeSpecCompiler().compile(
        [Requirement("R1", "Cache", "invalidate entries")],
        [],
    )
    assert not spec.complete
    assert [(gap.kind, gap.detail) for gap in spec.gaps] == [
        ("missing_component", "Cache")
    ]


def test_missing_expected_symbol_is_reported() -> None:
    spec = CodeSpecCompiler().compile(
        [Requirement("R1", "Cache", "invalidate entries", ("invalidate", "flush"))],
        [ComponentEvidence("Cache", "src/cache.py", symbols=("invalidate",))],
    )
    assert [gap.kind for gap in spec.gaps] == ["missing_symbol"]
    assert spec.gaps[0].detail == "flush"


def test_duplicate_requirement_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate requirement id"):
        CodeSpecCompiler().compile(
            [
                Requirement("R1", "Cache", "one"),
                Requirement("R1", "API", "two"),
            ],
            [ComponentEvidence("Cache", "src/cache.py")],
        )


def test_duplicate_component_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate component evidence"):
        CodeSpecCompiler().compile(
            [Requirement("R1", "Cache", "one")],
            [
                ComponentEvidence("Cache", "src/cache.py"),
                ComponentEvidence("Cache", "src/other.py"),
            ],
        )


def test_validate_reports_unimplemented_and_untested_components() -> None:
    spec = CodeSpecCompiler().compile(
        [
            Requirement("R1", "Cache", "invalidate entries"),
            Requirement("R2", "API", "return fresh values"),
        ],
        [
            ComponentEvidence("Cache", "src/cache.py"),
            ComponentEvidence("API", "src/api.py"),
        ],
    )
    report = CodeSpecCompiler().validate(
        spec,
        touched_components=("Cache",),
        test_names=("test_cache_invalidation",),
    )
    assert not report.consistent
    kinds = {(gap.requirement_id, gap.kind) for gap in report.gaps}
    assert ("R2", "unimplemented_component") in kinds
    assert ("R2", "untested_behavior") in kinds
    assert ("R1", "unimplemented_component") not in kinds


def test_validate_passes_when_all_components_and_tests_are_covered() -> None:
    spec = CodeSpecCompiler().compile(
        [Requirement("R1", "Cache", "invalidate entries")],
        [ComponentEvidence("Cache", "src/cache.py")],
    )
    report = CodeSpecCompiler().validate(
        spec,
        touched_components=("Cache",),
        test_names=("test_cache_invalidation",),
    )
    assert report.consistent
    assert report.gaps == ()
