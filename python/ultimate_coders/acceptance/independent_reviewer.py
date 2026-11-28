"""Independent L3 reviewer (#40) — isolated, context-bounded verification.

The ledger's *Rethinking Verification in Agent Harnesses* result replaces a
costly L2 self-check with an isolated L3 critic. This module is that critic's
deterministic core: it receives only the candidate patch, an independent test
report and an optional boundary contract. It deliberately has no trajectory
parameter, so the reviewer cannot inherit the worker's rationalisations.

Ledger item: #40 (arXiv:2610.34577, arXiv:2609.08472).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "BoundaryView",
    "Finding",
    "INDEPENDENT_REVIEWER_ENABLED_ENV",
    "IndependentReviewer",
    "ReviewDecision",
    "ReviewVerdict",
    "Severity",
    "TestReport",
    "independent_reviewer_enabled",
]

INDEPENDENT_REVIEWER_ENABLED_ENV = "UC_INDEPENDENT_REVIEWER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def independent_reviewer_enabled() -> bool:
    """Whether the opt-in L3 reviewer is enabled."""
    return (
        os.environ.get(INDEPENDENT_REVIEWER_ENABLED_ENV, "").strip().lower()
        in _TRUTHY
    )


class ReviewDecision(str, Enum):
    """Final L3 verdict."""

    APPROVE = "approve"
    NEEDS_CHANGES = "needs_changes"
    REJECT = "reject"


class Severity(str, Enum):
    """Finding severity."""

    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


@dataclass(frozen=True)
class Finding:
    """One deterministic review finding."""

    code: str
    message: str
    severity: Severity = Severity.BLOCKING
    path: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
            "path": self.path,
        }


@dataclass(frozen=True)
class TestReport:
    """Independent test evidence supplied by the acceptance layer."""

    passed: int
    failed: int
    total: int
    summary: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("passed", self.passed),
            ("failed", self.failed),
            ("total", self.total),
        ):
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.passed + self.failed > self.total:
            raise ValueError("passed + failed cannot exceed total")

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.failed == 0 and self.passed > 0


@dataclass(frozen=True)
class BoundaryView:
    """The subset of the boundary contract the isolated reviewer may see."""

    required_paths: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    editable_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "required_paths", tuple(str(path) for path in self.required_paths)
        )
        object.__setattr__(
            self, "protected_paths", tuple(str(path) for path in self.protected_paths)
        )
        object.__setattr__(
            self, "editable_paths", tuple(str(path) for path in self.editable_paths)
        )


@dataclass(frozen=True)
class ReviewVerdict:
    """Bounded, serialisable L3 review result."""

    decision: ReviewDecision
    findings: tuple[Finding, ...]
    confidence: float
    isolation: str
    reviewed_paths: tuple[str, ...]
    test_evidence: bool

    @property
    def approved(self) -> bool:
        return self.decision is ReviewDecision.APPROVE

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "findings": [finding.to_dict() for finding in self.findings],
            "confidence": self.confidence,
            "isolation": self.isolation,
            "reviewed_paths": list(self.reviewed_paths),
            "test_evidence": self.test_evidence,
        }


def changed_paths(patch: str) -> tuple[str, ...]:
    """Extract deterministic ``+++`` destinations from a unified diff."""
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue
        raw = line[4:].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith("b/"):
            raw = raw[2:]
        raw = raw.split("\t", 1)[0]
        if raw and raw not in paths:
            paths.append(raw)
    return tuple(paths)


def _is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return (
        normalized.startswith("tests/")
        or "/tests/" in normalized
        or normalized.startswith("test_")
        or "/test_" in normalized
        or normalized.endswith("_test.py")
        or normalized.endswith("_test.rs")
    )


class IndependentReviewer:
    """Pure L3 reviewer with a bounded finding list."""

    def __init__(self, *, max_findings: int = 8) -> None:
        if max_findings <= 0:
            raise ValueError("max_findings must be positive")
        self.max_findings = max_findings

    def review(
        self,
        patch: str,
        *,
        test_report: TestReport | None,
        boundary: BoundaryView | None = None,
        docs_only: bool = False,
    ) -> ReviewVerdict:
        """Review one patch without accepting any trajectory context."""
        findings: list[Finding] = []
        paths = changed_paths(patch)
        if not patch.strip():
            findings.append(Finding("empty_patch", "candidate patch is empty"))
        if not docs_only:
            if test_report is None:
                findings.append(
                    Finding(
                        "no_test_evidence",
                        "no independent test report was supplied",
                    )
                )
            elif test_report.failed > 0:
                findings.append(
                    Finding(
                        "tests_failed",
                        f"{test_report.failed} independent test(s) failed",
                    )
                )
            elif not test_report.ok:
                findings.append(
                    Finding(
                        "tests_not_executed",
                        "the independent test report has no passing execution",
                    )
                )
        for path in paths:
            if _is_test_path(path):
                findings.append(
                    Finding(
                        "test_file_modified",
                        "the patch modifies test files",
                        path=path,
                    )
                )
        for line in patch.splitlines():
            lower = line.lower()
            if (
                line.startswith("-")
                and not line.startswith("---")
                and "assert" in lower
            ):
                findings.append(
                    Finding(
                        "assertion_deleted",
                        "the patch deletes an assertion",
                    )
                )
                break
        for marker in (
            "pytest.mark.skip",
            "pytest.skip(",
            "unittest.skip",
            "pytest.mark.xfail",
            "xfail(",
            "--no-verify",
        ):
            if marker in patch.lower():
                findings.append(
                    Finding(
                        "verification_bypass",
                        f"the patch contains verification bypass marker {marker!r}",
                    )
                )
                break
        if boundary is not None:
            changed = set(paths)
            for path in boundary.protected_paths:
                if path in changed:
                    findings.append(
                        Finding(
                            "protected_path_modified",
                            "the patch modifies a protected path",
                            path=path,
                        )
                    )
            for path in boundary.required_paths:
                if path not in changed:
                    findings.append(
                        Finding(
                            "required_path_missing",
                            "the patch does not touch a required path",
                            severity=Severity.WARNING,
                            path=path,
                        )
                    )
        findings = findings[: self.max_findings]
        if any(finding.code == "required_path_missing" for finding in findings) and not any(
            finding.severity is Severity.BLOCKING for finding in findings
        ):
            decision = ReviewDecision.NEEDS_CHANGES
        elif findings:
            decision = ReviewDecision.REJECT
        else:
            decision = ReviewDecision.APPROVE
        confidence = max(0.5, 0.95 - 0.05 * len(findings))
        return ReviewVerdict(
            decision=decision,
            findings=tuple(findings),
            confidence=confidence,
            isolation="L3",
            reviewed_paths=paths,
            test_evidence=test_report is not None and test_report.ok,
        )

    def build_brief(
        self,
        patch: str,
        *,
        test_report: TestReport | None,
        boundary: BoundaryView | None = None,
        max_chars: int = 4000,
    ) -> str:
        """Build the isolated critic brief; no trajectory or free-text history."""
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        paths = changed_paths(patch)
        test_line = (
            "none"
            if test_report is None
            else (
                f"passed={test_report.passed} failed={test_report.failed} "
                f"total={test_report.total}"
            )
        )
        required = "-" if boundary is None else ", ".join(boundary.required_paths) or "-"
        protected = "-" if boundary is None else ", ".join(boundary.protected_paths) or "-"
        body = "\n".join(
            [
                "L3 isolated review",
                f"paths: {', '.join(paths) or '-'}",
                f"tests: {test_line}",
                f"required: {required}",
                f"protected: {protected}",
                "patch:",
                patch,
            ]
        )
        return body[:max_chars]

    def review_many(
        self,
        patches: Sequence[str],
        *,
        test_report: TestReport | None,
        boundary: BoundaryView | None = None,
    ) -> tuple[ReviewVerdict, ...]:
        """Review a bounded sequence of candidate patches."""
        return tuple(
            self.review(patch, test_report=test_report, boundary=boundary)
            for patch in patches
        )
