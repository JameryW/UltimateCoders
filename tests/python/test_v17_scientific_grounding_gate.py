"""Tests for the executable-context scientific grounding gate (ledger #66)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.scientific_grounding_gate import (
    SCIENTIFIC_GROUNDING_GATE_ENV,
    AlignmentReport,
    DomainClaim,
    GroundingError,
    RepoBinding,
    ScientificGroundingGate,
    SymbolBinding,
    scientific_grounding_enabled,
)


def _claim(
    *bindings: SymbolBinding,
    formula: str = "y = a * x + b",
    boundaries: tuple[str, ...] = ("x in [0, 1]",),
) -> DomainClaim:
    return DomainClaim(
        formula=formula,
        source="textbook",
        bindings=bindings
        or (SymbolBinding("a", "coef", "(1,)", "float32", "scale(x)"),),
        boundary_conditions=boundaries,
    )


def _repo() -> dict[str, RepoBinding]:
    return {
        "coef": RepoBinding("coef", "(1,)", "float32", "scale(x)"),
        "x": RepoBinding("x", "(B, T)", "float32", "forward(x)"),
    }


def test_flag_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SCIENTIFIC_GROUNDING_GATE_ENV, raising=False)
    assert not scientific_grounding_enabled()
    monkeypatch.setenv(SCIENTIFIC_GROUNDING_GATE_ENV, "1")
    assert scientific_grounding_enabled()


def test_aligned_claim_passes_and_renders() -> None:
    gate = ScientificGroundingGate()
    claim = _claim()
    report = gate.validate(claim, _repo())
    assert report.aligned
    assert report.anchors == 0
    assert report.to_dict()["missing_symbols"] == []
    rendered = gate.gate(claim, _repo())
    assert rendered is not None
    assert "y = a * x + b" in rendered
    assert "a->coef" in rendered
    assert "Boundary conditions" in rendered


def test_missing_symbol_is_not_aligned() -> None:
    gate = ScientificGroundingGate()
    report = gate.validate(
        _claim(SymbolBinding("z", "missing", "(B,)", "float32", "run(z)")),
        _repo(),
    )
    assert not report.aligned
    assert report.missing_symbols == ("missing",)
    assert gate.gate(
        _claim(SymbolBinding("z", "missing", "(B,)", "float32", "run(z)")),
        _repo(),
    ) is None


def test_shape_dtype_and_signature_mismatches_are_reported() -> None:
    gate = ScientificGroundingGate()
    report = gate.validate(
        _claim(
            SymbolBinding("a", "coef", "(2,)", "float32", "scale(x)"),
            SymbolBinding("x", "x", "(B, T)", "float16", "forward(x)"),
            SymbolBinding("z", "coef", "(1,)", "float32", "other(z)"),
        ),
        _repo(),
    )
    assert not report.aligned
    assert report.shape_mismatches == ("coef: (2,) != (1,)",)
    assert report.dtype_mismatches == ("x: float16 != float32",)
    assert report.signature_mismatches == ("coef: other(z) != scale(x)",)


def test_claim_requires_bindings_and_boundaries() -> None:
    with pytest.raises(GroundingError):
        DomainClaim(formula="f", source="s", bindings=(), boundary_conditions=("x",))
    with pytest.raises(GroundingError):
        DomainClaim(
            formula="f",
            source="s",
            bindings=(SymbolBinding("a", "a"),),
            boundary_conditions=(),
        )
    with pytest.raises(GroundingError):
        DomainClaim(
            formula="",
            source="s",
            bindings=(SymbolBinding("a", "a"),),
            boundary_conditions=("x",),
        )


def test_empty_repository_is_rejected() -> None:
    with pytest.raises(GroundingError):
        ScientificGroundingGate().validate(_claim(), {})


def test_repo_binding_requires_all_fields() -> None:
    with pytest.raises(GroundingError):
        RepoBinding("", "(1,)", "float32", "run(x)")


def test_alignment_report_anchors() -> None:
    blocked = AlignmentReport(False, ("x",), (), (), (), "missing")
    assert blocked.anchors == 1
