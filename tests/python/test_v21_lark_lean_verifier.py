"""Tests for ledger item #86 (LARK Lean 4 segmented verification)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.lark_lean_verifier import (
    FAILED,
    LEVEL_2,
    PROVED,
    UNPROVED,
    KernelSegment,
    LarkLeanError,
    LarkLeanVerifier,
    MachineModel,
    SegmentProof,
    lark_lean_verifier_enabled,
)


def model() -> MachineModel:
    return MachineModel("cuda", "GPU machine model v1", ("load", "store"))


def segment(name: str, *postconditions: str) -> KernelSegment:
    return KernelSegment(name, f"kernel {name}", tuple(postconditions or ("result == expected",)))


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_LARK_LEAN_VERIFIER", raising=False)
    assert not lark_lean_verifier_enabled()
    monkeypatch.setenv("UC_LARK_LEAN_VERIFIER", "true")
    assert lark_lean_verifier_enabled()


def test_machine_model_is_required() -> None:
    with pytest.raises(LarkLeanError, match="machine model"):
        LarkLeanVerifier().verify((segment("a"),))


def test_level_one_verifies_all_proved_segments() -> None:
    verifier = LarkLeanVerifier()
    verifier.register_machine_model(model())
    verifier.record_proof(SegmentProof("a", PROVED, ("post-a",)))
    report = verifier.verify((segment("a"),))
    assert report.verified
    assert report.proved_segments == 1
    assert report.to_dict()["proved_segments"] == 1


def test_missing_proof_remains_unproved() -> None:
    verifier = LarkLeanVerifier()
    verifier.register_machine_model(model())
    report = verifier.verify((segment("a"),))
    assert not report.verified
    assert report.segments[0].status == UNPROVED


def test_level_two_requires_global_equivalence() -> None:
    verifier = LarkLeanVerifier(level=LEVEL_2)
    verifier.register_machine_model(model())
    verifier.record_proof(SegmentProof("a", PROVED, ("post-a",)))
    assert not verifier.verify((segment("a"),)).verified
    assert verifier.verify((segment("a"),), global_equivalence=True).verified


def test_failed_proof_blocks_verification() -> None:
    verifier = LarkLeanVerifier()
    verifier.register_machine_model(model())
    verifier.record_proof(SegmentProof("a", FAILED, ("post-a",), "counterexample"))
    assert not verifier.verify((segment("a"),)).verified


def test_proof_without_obligations_is_not_counted() -> None:
    verifier = LarkLeanVerifier()
    verifier.register_machine_model(model())
    verifier.record_proof(SegmentProof("a", PROVED, ()))
    assert not verifier.verify((segment("a"),)).verified


def test_duplicate_proofs_are_rejected() -> None:
    verifier = LarkLeanVerifier()
    verifier.record_proof(SegmentProof("a", PROVED, ("post",)))
    with pytest.raises(LarkLeanError, match="duplicate proof"):
        verifier.record_proof(SegmentProof("a", PROVED, ("post",)))


def test_duplicate_segments_are_rejected() -> None:
    verifier = LarkLeanVerifier()
    verifier.register_machine_model(model())
    with pytest.raises(LarkLeanError, match="unique"):
        verifier.verify((segment("a"), segment("a")))


def test_unknown_dependency_is_rejected() -> None:
    verifier = LarkLeanVerifier()
    verifier.register_machine_model(model())
    with pytest.raises(LarkLeanError, match="unknown segment dependency"):
        verifier.verify((KernelSegment("a", "source", ("post",), ("missing",)),))


def test_reset_clears_proofs() -> None:
    verifier = LarkLeanVerifier()
    verifier.register_machine_model(model())
    verifier.record_proof(SegmentProof("a", PROVED, ("post",)))
    verifier.reset()
    assert not verifier.verify((segment("a"),)).verified
