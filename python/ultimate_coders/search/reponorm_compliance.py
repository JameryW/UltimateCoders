"""RepoNorm + SWE-CC (#46): repository policy extraction and compliance audit.

Cross-repository contract propagation lives in the Rust coordinator. This module
covers the other half of item #46: repository governance. RepoNorm distils
explicit and implicit repository norms from documentation and git history into
atomic, machine-checkable policy packages. SWE-CC's finding is that 43.1% of
functionally correct patches still violate a project policy, so the audit is
deterministic and runs before an intermediate or final merge.

Opt-in flag: ``UC_REPONORM_COMPLIANCE``. Pure bookkeeping, no network or LLM.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ComplianceReport",
    "ComplianceRuleError",
    "ComplianceViolation",
    "NormCategory",
    "NormPackage",
    "NormRule",
    "NormSeverity",
    "REPONORM_COMPLIANCE_ENV",
    "RepoNormCompliance",
    "RepoNormExtractor",
    "SWE_CC_MACHINE_CHECKABLE_POLICIES",
    "reponorm_compliance_enabled",
]

REPONORM_COMPLIANCE_ENV = "UC_REPONORM_COMPLIANCE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: SWE-CC reports 823 machine-checkable policy classes across its repositories.
SWE_CC_MACHINE_CHECKABLE_POLICIES = 823


def reponorm_compliance_enabled() -> bool:
    """Return whether RepoNorm compliance auditing is enabled."""

    return os.environ.get(REPONORM_COMPLIANCE_ENV, "").strip().lower() in _TRUTHY


class ComplianceRuleError(ValueError):
    """Raised when a policy rule or package is malformed."""


class NormCategory(str, Enum):
    ARCHITECTURE = "architecture"
    API = "api"
    TESTING = "testing"
    SECURITY = "security"
    DOCUMENTATION = "documentation"
    STYLE = "style"
    DEPENDENCY = "dependency"
    RELEASE = "release"
    ERROR_HANDLING = "error_handling"
    CONFIGURATION = "configuration"
    PERFORMANCE = "performance"
    LICENSING = "licensing"
    OTHER = "other"


class NormSeverity(str, Enum):
    ADVISORY = "advisory"
    REQUIRED = "required"
    BLOCKING = "blocking"


_CATEGORY_CUES: tuple[tuple[NormCategory, tuple[str, ...]], ...] = (
    (NormCategory.TESTING, ("test", "coverage", "assert", "fixture")),
    (NormCategory.SECURITY, ("secret", "credential", "auth", "token", "sanitize", "security")),
    (NormCategory.DOCUMENTATION, ("doc", "readme", "changelog", "comment")),
    (NormCategory.DEPENDENCY, ("dependency", "dependencies", "package", "import")),
    (NormCategory.RELEASE, ("release", "changelog", "version", "tag")),
    (NormCategory.ERROR_HANDLING, ("error", "exception", "panic", "unwrap", "retry")),
    (NormCategory.CONFIGURATION, ("config", "configuration", "environment", "env")),
    (NormCategory.PERFORMANCE, ("performance", "latency", "allocation", "complexity")),
    (NormCategory.LICENSING, ("license", "copyright", "header")),
    (NormCategory.API, ("api", "public", "signature", "contract", "interface")),
    (NormCategory.ARCHITECTURE, ("module", "layer", "crate", "architecture", "service")),
    (NormCategory.STYLE, ("format", "lint", "style", "clippy", "ruff")),
)

_POLICY_CUES = (
    " must ",
    " must not ",
    " mustn't ",
    " should ",
    " should not ",
    " never ",
    " do not ",
    " don't ",
    " required ",
    " prohibited ",
    " always ",
)
_FORBIDDEN_CUES = ("must not", "should not", "never", "do not", "don't", "prohibited")


def _infer_category(text: str) -> NormCategory:
    lowered = text.lower()
    for category, cues in _CATEGORY_CUES:
        if any(cue in lowered for cue in cues):
            return category
    return NormCategory.OTHER


@dataclass(frozen=True)
class NormRule:
    """One atomic repository policy rule."""

    rule_id: str
    category: NormCategory
    text: str
    severity: NormSeverity = NormSeverity.REQUIRED
    source: str = "manual"
    forbidden_pattern: str = ""
    required_pattern: str = ""
    machine_checkable: bool = True

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise ComplianceRuleError("rule_id must not be empty")
        if not self.text.strip():
            raise ComplianceRuleError("rule text must not be empty")
        for label, pattern in (
            ("forbidden_pattern", self.forbidden_pattern),
            ("required_pattern", self.required_pattern),
        ):
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ComplianceRuleError(f"invalid {label}: {exc}") from exc
        if not (self.forbidden_pattern or self.required_pattern):
            object.__setattr__(self, "machine_checkable", False)

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "category": self.category.value,
            "text": self.text,
            "severity": self.severity.value,
            "source": self.source,
            "forbidden_pattern": self.forbidden_pattern,
            "required_pattern": self.required_pattern,
            "machine_checkable": self.machine_checkable,
        }


@dataclass(frozen=True)
class NormPackage:
    """A repository's exported norm package."""

    repo: str
    rules: tuple[NormRule, ...]

    def __post_init__(self) -> None:
        if not self.repo.strip():
            raise ComplianceRuleError("repo must not be empty")
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ComplianceRuleError("rule ids must be unique within a package")

    def by_category(self, category: NormCategory) -> tuple[NormRule, ...]:
        return tuple(rule for rule in self.rules if rule.category is category)

    def machine_checkable_count(self) -> int:
        return sum(1 for rule in self.rules if rule.machine_checkable)


@dataclass(frozen=True)
class ComplianceViolation:
    """One deterministic policy violation."""

    rule_id: str
    category: NormCategory
    severity: NormSeverity
    message: str
    evidence: str
    rule_text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "category": self.category.value,
            "severity": self.severity.value,
            "message": self.message,
            "evidence": self.evidence,
            "rule_text": self.rule_text,
        }


@dataclass(frozen=True)
class ComplianceReport:
    """The deterministic audit result for one patch."""

    repo: str
    checked: int
    skipped: int
    violations: tuple[ComplianceViolation, ...]
    changed_paths: tuple[str, ...] = ()

    @property
    def compliant(self) -> bool:
        return not self.violations

    @property
    def blocking_violations(self) -> tuple[ComplianceViolation, ...]:
        return tuple(
            violation
            for violation in self.violations
            if violation.severity in (NormSeverity.REQUIRED, NormSeverity.BLOCKING)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "checked": self.checked,
            "skipped": self.skipped,
            "compliant": self.compliant,
            "changed_paths": list(self.changed_paths),
            "violations": [violation.to_dict() for violation in self.violations],
        }


class RepoNormExtractor:
    """Distil policy-looking lines from docs and git history."""

    def extract(
        self,
        repo: str,
        docs: Mapping[str, str] | None = None,
        history: Sequence[str] = (),
    ) -> NormPackage:
        """Extract atomic rules from documentation and commit-message history."""

        if not repo.strip():
            raise ComplianceRuleError("repo must not be empty")
        rules: list[NormRule] = []
        counter = 0
        sources: list[tuple[str, str]] = []
        for path, content in (docs or {}).items():
            for line in content.splitlines():
                sources.append((path, line))
        for message in history:
            sources.append(("git-history", message))
        for source, raw in sources:
            line = raw.strip().lstrip("*-+# ").strip()
            if not line:
                continue
            lowered = f" {line.lower()} "
            if not any(cue in lowered for cue in _POLICY_CUES):
                continue
            counter += 1
            category = _infer_category(line)
            severity = (
                NormSeverity.BLOCKING
                if any(cue in lowered for cue in _FORBIDDEN_CUES)
                else NormSeverity.REQUIRED
            )
            rules.append(
                NormRule(
                    rule_id=f"{repo}:{counter:03d}",
                    category=category,
                    text=line,
                    severity=severity,
                    source=source,
                )
            )
        return NormPackage(repo=repo, rules=tuple(rules))


class RepoNormCompliance:
    """Deterministic policy audit over a plain-text patch."""

    def audit(
        self,
        patch: str,
        package: NormPackage,
        changed_paths: Sequence[str] = (),
    ) -> ComplianceReport:
        """Audit a patch against every machine-checkable rule in a package."""

        lines = patch.splitlines()
        checked = 0
        skipped = 0
        violations: list[ComplianceViolation] = []
        for rule in package.rules:
            if not rule.machine_checkable:
                skipped += 1
                continue
            checked += 1
            if rule.forbidden_pattern:
                pattern = re.compile(rule.forbidden_pattern)
                for line_number, line in enumerate(lines, start=1):
                    if pattern.search(line):
                        violations.append(
                            ComplianceViolation(
                                rule_id=rule.rule_id,
                                category=rule.category,
                                severity=rule.severity,
                                message=(
                                    f"{rule.rule_id} forbids {rule.forbidden_pattern!r}"
                                ),
                                evidence=f"{line_number}:{line.strip()[:200]}",
                                rule_text=rule.text,
                            )
                        )
                        break
            elif rule.required_pattern:
                pattern = re.compile(rule.required_pattern)
                if not any(pattern.search(line) for line in lines):
                    violations.append(
                        ComplianceViolation(
                            rule_id=rule.rule_id,
                            category=rule.category,
                            severity=rule.severity,
                            message=(
                                f"{rule.rule_id} requires {rule.required_pattern!r}"
                            ),
                            evidence="missing from patch",
                            rule_text=rule.text,
                        )
                    )
        return ComplianceReport(
            repo=package.repo,
            checked=checked,
            skipped=skipped,
            violations=tuple(violations),
            changed_paths=tuple(changed_paths),
        )
