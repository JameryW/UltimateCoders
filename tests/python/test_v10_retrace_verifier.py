"""Tests for the v10 RETRACE verifier (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.retrace_verifier import (
    RETRACE_ENABLED_ENV,
    STATUS_AGREE,
    STATUS_CONTRADICTORY,
    STATUS_MISMATCH,
    BlindReconstruction,
    RetraceVerifier,
    TraceRationale,
    retrace_enabled,
)


def test_forward_produces_rationale_clauses() -> None:
    verifier = RetraceVerifier()
    rationale = verifier.forward(
        {
            "title": "Fix cache locking",
            "description": "The cache must release the lock in a finally block.",
            "acceptance_criteria": ["The fix must not skip validation."],
        },
        {
            "steps": ["read cache.py", "add release path"],
            "rationale": ["The cache must release the lock."],
        },
    )
    assert isinstance(rationale, TraceRationale)
    assert rationale.clauses
    joined = " ".join(rationale.clauses).lower()
    assert "release the lock" in joined
    assert "skip validation" in joined
    assert rationale.trajectory_steps == 2


def test_backward_is_blind_to_issue_text() -> None:
    verifier = RetraceVerifier()
    patch = (
        "--- a/cache.py\n+++ b/cache.py\n@@ -1,2 +1,4 @@\n"
        " def get(key):\n+    with self._lock:\n+        return self._cache[key]\n"
    )
    reconstruction = verifier.backward(patch, {"cache.py": ["get"]})
    assert isinstance(reconstruction, BlindReconstruction)
    assert reconstruction.used_issue_text is False
    assert reconstruction.claims
    assert reconstruction.files == ("cache.py",)
    assert reconstruction.patch_digest

    forced = verifier.backward(patch, {"cache.py": ["get"]}, include_issue=True)
    assert forced.used_issue_text is False
    assert forced.claims == reconstruction.claims


def test_three_way_agreement() -> None:
    verifier = RetraceVerifier()
    rationale = TraceRationale(clauses=("release the lock", "validate the token"))
    reconstruction = BlindReconstruction(claims=("release the lock", "validate the token"))
    verdict = verifier.arbitrate(rationale, reconstruction)
    assert verdict.status == STATUS_AGREE
    assert verdict.passed is True
    assert verdict.missing == ()
    assert verdict.contradictory == ()
    assert len(verdict.matched) == 2


def test_mismatch_flags_missing_requirement_and_repair_instruction() -> None:
    verifier = RetraceVerifier()
    rationale = TraceRationale(clauses=("release the lock", "validate the token"))
    reconstruction = BlindReconstruction(claims=("release the lock",))
    verdict = verifier.arbitrate(rationale, reconstruction)
    assert verdict.status == STATUS_MISMATCH
    assert verdict.passed is False
    assert "validate the token" in verdict.missing
    assert "validate the token" in verdict.repair_instruction
    assert "targeted repair" in verdict.repair_instruction.lower()


def test_contradiction_is_detected() -> None:
    verifier = RetraceVerifier()
    rationale = TraceRationale(clauses=("must not skip validation",))
    reconstruction = BlindReconstruction(claims=("skip validation",))
    verdict = verifier.arbitrate(rationale, reconstruction)
    assert verdict.status == STATUS_CONTRADICTORY
    assert verdict.passed is False
    assert verdict.contradictory == ("must not skip validation",)


def test_patch_evidence_can_satisfy_claim() -> None:
    verifier = RetraceVerifier()
    rationale = TraceRationale(clauses=("release the lock",))
    reconstruction = BlindReconstruction(claims=())
    patch = "--- a/cache.py\n+++ b/cache.py\n@@ -1 +1,2 @@\n+    lock.release()\n"
    verdict = verifier.arbitrate(rationale, reconstruction, patch_evidence=patch)
    assert verdict.status == STATUS_AGREE
    assert verdict.passed is True


def test_backward_does_not_leak_issue_text_through_arbitration() -> None:
    verifier = RetraceVerifier()
    rationale = TraceRationale(clauses=("release the lock",))
    tainted = BlindReconstruction(claims=("release the lock",), used_issue_text=True)
    verdict = verifier.arbitrate(rationale, tainted)
    assert verdict.status == STATUS_CONTRADICTORY
    assert "issue text" in verdict.repair_instruction


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_retrace_flag_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(RETRACE_ENABLED_ENV, value)
    assert retrace_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "banana"])
def test_retrace_flag_falsey(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(RETRACE_ENABLED_ENV, value)
    assert retrace_enabled() is False
