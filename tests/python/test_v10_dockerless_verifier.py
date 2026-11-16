"""Tests for the v10 Dockerless agentic verifier (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.dockerless_verifier import (
    DOCKERLESS_VERIFIER_ENV,
    DockerlessAgenticVerifier,
    DockerlessReport,
    PatchSignal,
    dockerless_verifier_enabled,
)

_PASSING_PATCH = "--- a/pkg.py\n+++ b/pkg.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n+    return a + b\n"

_LOCK_LEAK_PATCH = (
    "--- a/cache.py\n+++ b/cache.py\n@@ -1,2 +1,4 @@\n"
    " def get(key):\n+    self._lock.acquire()\n+    return self._cache[key]\n"
)


def test_passing_patch_scores_100() -> None:
    report = DockerlessAgenticVerifier().verify(_PASSING_PATCH, repo_index={})
    assert isinstance(report, DockerlessReport)
    assert report.score == 100
    assert report.passed is True
    assert report.missing_evidence == ()
    assert all(report.checks.values())


def test_missing_lock_release_fails() -> None:
    report = DockerlessAgenticVerifier().verify(_LOCK_LEAK_PATCH, repo_index={})
    assert report.checks[PatchSignal.LOCK_RELEASE.value] is False
    assert any("lock release" in item.lower() for item in report.missing_evidence)
    assert report.passed is False
    assert report.score < 70


def test_signature_mismatch_fails() -> None:
    patch = (
        "--- a/pkg.py\n+++ b/pkg.py\n@@ -1,2 +1,2 @@\n"
        "-def process(data):\n+def process(data, timeout):\n"
    )
    report = DockerlessAgenticVerifier().verify(patch, repo_index={})
    assert report.checks[PatchSignal.SIGNATURE_COMPATIBILITY.value] is False
    assert any("signature mismatch" in item for item in report.missing_evidence)
    assert report.passed is False


def test_repo_index_signature_check() -> None:
    patch = "--- a/pkg.py\n+++ b/pkg.py\n@@ -1 +1,2 @@\n+def process(data):\n"
    report = DockerlessAgenticVerifier().verify(
        patch,
        repo_index={"pkg.py": {"process": "def process(data, timeout)"}},
    )
    assert report.checks[PatchSignal.SIGNATURE_COMPATIBILITY.value] is False


def test_boundary_condition_coverage() -> None:
    repo_index = {"boundary_conditions": ["empty input"]}
    covered = (
        "--- a/pkg.py\n+++ b/pkg.py\n@@ -1 +1,4 @@\n"
        " def first(items):\n+    if not items:\n+        return None\n"
    )
    report = DockerlessAgenticVerifier().verify(covered, repo_index=repo_index)
    assert report.checks[PatchSignal.BOUNDARY_CONDITIONS.value] is True

    uncovered = "--- a/pkg.py\n+++ b/pkg.py\n@@ -1 +1,2 @@\n+    return items[0]\n"
    report = DockerlessAgenticVerifier().verify(uncovered, repo_index=repo_index)
    assert report.checks[PatchSignal.BOUNDARY_CONDITIONS.value] is False
    assert any("boundary condition" in item for item in report.missing_evidence)


def test_threshold_is_configurable() -> None:
    strict = DockerlessAgenticVerifier(threshold=70)
    lenient = DockerlessAgenticVerifier(pass_threshold=60)
    strict_report = strict.verify(_LOCK_LEAK_PATCH, repo_index={})
    lenient_report = lenient.verify(_LOCK_LEAK_PATCH, repo_index={})
    assert strict_report.passed is False
    assert lenient_report.passed is True
    assert lenient_report.threshold == 60


def test_call_chain_signature_check() -> None:
    patch = (
        "--- a/pkg.py\n+++ b/pkg.py\n@@ -1,2 +1,2 @@\n"
        "-def process(data):\n+def process(data, timeout):\n"
    )
    report = DockerlessAgenticVerifier().verify(
        patch,
        repo_index={},
        call_chain=[{"name": "process", "args": ["data"]}],
    )
    assert report.checks[PatchSignal.SIGNATURE_COMPATIBILITY.value] is False


def test_report_to_dict_and_summary() -> None:
    report = DockerlessAgenticVerifier().verify(_PASSING_PATCH, repo_index={})
    data = report.to_dict()
    assert data["score"] == 100
    assert "PASS" in report.summary()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_dockerless_flag_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(DOCKERLESS_VERIFIER_ENV, value)
    assert dockerless_verifier_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "banana"])
def test_dockerless_flag_falsey(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(DOCKERLESS_VERIFIER_ENV, value)
    assert dockerless_verifier_enabled() is False
