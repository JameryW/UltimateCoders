"""Foresight-SWE + SWE-Residual (#45): residual taint and anticipation tests.

Two failure modes are coupled in the ledger:

* A later attempt inherits a failed workspace whose dirty files encode the
  previous attempt's misunderstanding. That residual state creates an
  *acceptance bias*: the new attempt accepts the old diff instead of re-deriving
  the change from the issue.
* Coding agents rarely catch defects they introduce in their own patch. The
  Foresight-SWE result is that agents solve 91.23% of tasks when told the bug,
  but only 6.13–24.53% when asked to anticipate it.

The auditor implements a zero-trust residual re-anchor (dirty files are marked
``UNVERIFIED_RESIDUAL_TAINT`` and must be re-audited against the issue) and a
deterministic anticipation-test synthesizer for four call-chain boundaries.

Opt-in flag: ``UC_FORESIGHT_RESIDUAL_AUDITOR``. Pure bookkeeping, no LLM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "AnticipationTest",
    "BoundaryKind",
    "FORESIGHT_RESIDUAL_AUDITOR_ENV",
    "ForesightAuditor",
    "ForesightReport",
    "ForesightResidualAuditor",
    "ForesightSynthesizer",
    "ResidualAudit",
    "ResidualAuditError",
    "ResidualFile",
    "ResidualTaint",
    "ResidualWorkspace",
    "UNVERIFIED_RESIDUAL_TAINT",
    "foresight_residual_auditor_enabled",
]

FORESIGHT_RESIDUAL_AUDITOR_ENV = "UC_FORESIGHT_RESIDUAL_AUDITOR"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
UNVERIFIED_RESIDUAL_TAINT = "UNVERIFIED_RESIDUAL_TAINT"


def foresight_residual_auditor_enabled() -> bool:
    """Return whether residual auditing is enabled."""

    return (
        os.environ.get(FORESIGHT_RESIDUAL_AUDITOR_ENV, "").strip().lower() in _TRUTHY
    )


class ResidualAuditError(ValueError):
    """Raised when a residual workspace or audit input is malformed."""


@dataclass(frozen=True)
class ResidualFile:
    """One file left behind by a previous attempt."""

    path: str
    content: str = ""
    dirty: bool = True
    lines_changed: int = 0

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ResidualAuditError("residual file path must not be empty")
        if self.lines_changed < 0:
            raise ResidualAuditError("lines_changed must be non-negative")
        object.__setattr__(self, "path", self.path.strip().replace("\\", "/"))


@dataclass(frozen=True)
class ResidualWorkspace:
    """The residual file tree inherited by the next attempt."""

    attempt_id: str
    files: tuple[ResidualFile, ...]
    base_commit: str = ""

    def __post_init__(self) -> None:
        if not self.attempt_id.strip():
            raise ResidualAuditError("attempt_id must not be empty")
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ResidualAuditError("residual file paths must be unique")

    @property
    def dirty_files(self) -> tuple[ResidualFile, ...]:
        return tuple(file for file in self.files if file.dirty)

    @property
    def dirty_ratio(self) -> float:
        if not self.files:
            return 0.0
        return len(self.dirty_files) / len(self.files)


@dataclass(frozen=True)
class ResidualTaint:
    """The taint label attached to one residual file."""

    path: str
    label: str = UNVERIFIED_RESIDUAL_TAINT
    lines_changed: int = 0
    issue_overlap: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "label": self.label,
            "lines_changed": self.lines_changed,
            "issue_overlap": self.issue_overlap,
        }


@dataclass(frozen=True)
class ResidualAudit:
    """The zero-trust result for one inherited workspace."""

    attempt_id: str
    tainted: tuple[ResidualTaint, ...]
    dirty_ratio: float
    issue_overlap_terms: tuple[str, ...]
    coverage_gap: tuple[str, ...]
    requires_reanchor: bool
    reason: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.dirty_ratio <= 1.0:
            raise ResidualAuditError("dirty_ratio must be in [0, 1]")

    @property
    def clean(self) -> bool:
        return not self.tainted

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "tainted": [taint.to_dict() for taint in self.tainted],
            "dirty_ratio": self.dirty_ratio,
            "issue_overlap_terms": list(self.issue_overlap_terms),
            "coverage_gap": list(self.coverage_gap),
            "requires_reanchor": self.requires_reanchor,
            "reason": self.reason,
            "clean": self.clean,
        }


def _normalize_terms(issue_terms: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    terms = []
    for term in issue_terms:
        normalized = term.strip().lower()
        if normalized and normalized not in terms:
            terms.append(normalized)
    return tuple(terms)


class ForesightAuditor:
    """Mark inherited dirty files and compute issue coverage gaps."""

    def __init__(
        self,
        taint_threshold: float = 0.5,
        overlap_threshold: int = 1,
    ) -> None:
        if not 0.0 <= taint_threshold <= 1.0:
            raise ResidualAuditError("taint_threshold must be in [0, 1]")
        if overlap_threshold < 1:
            raise ResidualAuditError("overlap_threshold must be >= 1")
        self.taint_threshold = taint_threshold
        self.overlap_threshold = overlap_threshold

    def audit(
        self,
        workspace: ResidualWorkspace,
        issue_terms: tuple[str, ...] | list[str] = (),
    ) -> ResidualAudit:
        """Audit an inherited workspace before a new attempt starts editing."""

        terms = _normalize_terms(issue_terms)
        tainted: list[ResidualTaint] = []
        overlap_terms: list[str] = []
        combined_text = "\n".join(
            f"{file.path}\n{file.content}".lower() for file in workspace.files
        )
        for file in workspace.files:
            if not file.dirty:
                continue
            haystack = f"{file.path}\n{file.content}".lower()
            matches = tuple(term for term in terms if term in haystack)
            for term in matches:
                if term not in overlap_terms:
                    overlap_terms.append(term)
            tainted.append(
                ResidualTaint(
                    path=file.path,
                    lines_changed=file.lines_changed,
                    issue_overlap=len(matches) >= self.overlap_threshold,
                )
            )
        coverage_gap = tuple(term for term in terms if term not in combined_text)
        requires_reanchor = bool(tainted)
        if not tainted:
            reason = "no residual dirty files were inherited"
        elif len(tainted) / max(1, len(workspace.files)) >= self.taint_threshold:
            reason = (
                f"{len(tainted)} of {len(workspace.files)} files are residual and "
                "require an independent issue re-anchor"
            )
        else:
            reason = "residual files still require independent verification"
        return ResidualAudit(
            attempt_id=workspace.attempt_id,
            tainted=tuple(tainted),
            dirty_ratio=workspace.dirty_ratio,
            issue_overlap_terms=tuple(overlap_terms),
            coverage_gap=coverage_gap,
            requires_reanchor=requires_reanchor,
            reason=reason,
        )


class BoundaryKind(str, Enum):
    """The four anticipation-test boundaries from the ledger."""

    NULL = "null"
    CONCURRENCY = "concurrency"
    TYPE_OVERFLOW = "type_overflow"
    CONTRACT_COMPATIBILITY = "contract_compatibility"


@dataclass(frozen=True)
class AnticipationTest:
    """A deterministic, human-readable anticipation test."""

    kind: BoundaryKind
    target: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "target": self.target,
            "description": self.description,
        }


_NULL_STEMS = ("parse", "load", "read", "decode", "lookup", "find", "get_")
_CONCURRENCY_STEMS = ("lock", "cache", "queue", "thread", "async", "concurrent", "mutex")
_TYPE_STEMS = ("count", "size", "index", "int", "len", "offset", "capacity", "id")


class ForesightSynthesizer:
    """Synthesize anticipation tests for changed functions."""

    def _classify(self, function: str) -> BoundaryKind:
        name = function.lower()
        if any(stem in name for stem in _NULL_STEMS):
            return BoundaryKind.NULL
        if any(stem in name for stem in _CONCURRENCY_STEMS):
            return BoundaryKind.CONCURRENCY
        if any(stem in name for stem in _TYPE_STEMS):
            return BoundaryKind.TYPE_OVERFLOW
        return BoundaryKind.CONTRACT_COMPATIBILITY

    def synthesize(
        self,
        changed_functions: tuple[str, ...] | list[str],
        issue_terms: tuple[str, ...] | list[str] = (),
    ) -> tuple[AnticipationTest, ...]:
        """Create one anticipation test per changed function."""

        terms = _normalize_terms(issue_terms)
        tests: list[AnticipationTest] = []
        for raw in changed_functions:
            function = raw.strip()
            if not function:
                raise ResidualAuditError("changed function name must not be empty")
            kind = self._classify(function)
            context = f" (issue terms: {', '.join(terms)})" if terms else ""
            descriptions = {
                BoundaryKind.NULL: (
                    f"Call {function} with null/empty inputs and assert the documented "
                    f"fallback instead of a crash{context}."
                ),
                BoundaryKind.CONCURRENCY: (
                    f"Exercise {function} under two concurrent callers and assert the "
                    f"lock/order invariant survives{context}."
                ),
                BoundaryKind.TYPE_OVERFLOW: (
                    f"Drive {function} past its integer/size boundary and assert the "
                    f"overflow guard or documented saturation{context}."
                ),
                BoundaryKind.CONTRACT_COMPATIBILITY: (
                    f"Check {function}'s callers against the unchanged public contract "
                    f"and assert no signature/semantic regression{context}."
                ),
            }
            tests.append(
                AnticipationTest(
                    kind=kind,
                    target=function,
                    description=descriptions[kind],
                )
            )
        return tuple(tests)


@dataclass(frozen=True)
class ForesightReport:
    """The residual audit plus the anticipation suite for one attempt."""

    residual: ResidualAudit
    anticipation: tuple[AnticipationTest, ...]

    @property
    def foresight_ready(self) -> bool:
        return bool(self.anticipation)

    def to_dict(self) -> dict[str, object]:
        return {
            "residual": self.residual.to_dict(),
            "anticipation": [test.to_dict() for test in self.anticipation],
            "foresight_ready": self.foresight_ready,
        }


class ForesightResidualAuditor:
    """Combined residual-taint and anticipation-test gate."""

    def __init__(
        self,
        auditor: ForesightAuditor | None = None,
        synthesizer: ForesightSynthesizer | None = None,
    ) -> None:
        self.auditor = auditor or ForesightAuditor()
        self.synthesizer = synthesizer or ForesightSynthesizer()

    def evaluate(
        self,
        workspace: ResidualWorkspace,
        changed_functions: tuple[str, ...] | list[str],
        issue_terms: tuple[str, ...] | list[str] = (),
    ) -> ForesightReport:
        """Audit residual state and synthesize tests before accepting a patch."""

        residual = self.auditor.audit(workspace, issue_terms)
        anticipation = self.synthesizer.synthesize(changed_functions, issue_terms)
        return ForesightReport(residual=residual, anticipation=anticipation)
