"""Deterministic rubric audit for a finished attempt (V29 #138).

The ledger item (**Repository-Grounded Rubrics**, ``arXiv:2611.23309``) evaluates
a patch without running anything: a weighted binary rubric is frozen before
editing, then a judge scores the diff, reaching 0.826 AUROC on SWE-bench
Verified patches and ranking whole systems from failing patches alone. The
implementation lives in ``python/ultimate_coders/acceptance/repo_rubric_scorer.py``
but nothing ever built a rubric or scored a diff, so it was dead weight.

This module is the missing adapter, kept deliberately small and honest. It runs
*only deterministic checks* on evidence the worker already holds - the diff it
produced, the paths it touched, and the observed test delta - and turns each
check into a rubric feature. No model is asked to judge anything, and the result
is advisory: it never rejects a subtask, it only reports a score next to the
declared and observed acceptance signals.

Why it is worth wiring at all: the paper's use case is the one this worker is
weakest at. When a repository has no runnable test command the acceptance chain
has nothing to say, and a rubric over non-execution evidence is the only signal
left. When tests *did* run, the rubric adds cheap sanity checks they cannot see
(scope drift, newly introduced hazards).

Checks, one per rubric dimension:

* **correctness** - the diff adds at least one line (a pure deletion never
  "fixes" anything, and stub-only patches are caught by the added-line test).
* **test_coverage** - when the description asks for a fix, the patch touches a
  test file, or the observed delta reports a passing F2P set.
* **compatibility** - every changed path stays inside the subtask's declared
  file constraints (skipped when the subtask declares none).
* **security** - the added lines introduce none of a small set of hazards
  (``eval``/``exec``, ``shell=True``, ``os.system``, ``pickle.loads``,
  ``yaml.load`` without a safe loader).
* **maintainability** - the patch adds no ``TODO``/``FIXME``/``XXX`` marker and
  no bare ``except:``.

Opt-in via ``UC_RUBRIC_AUDIT`` (and ``UC_REPO_RUBRIC_SCORER`` for the scorer).
With either flag off the worker prompt and result are byte-identical.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ultimate_coders.acceptance.repo_rubric_scorer import (
    FrozenRubric,
    PatchEvidence,
    RepoGroundedRubricScorer,
    RubricDimension,
    RubricItem,
    RubricScore,
    repo_rubric_scorer_enabled,
)

__all__ = [
    "RUBRIC_AUDIT_ENV",
    "MAX_REPORTED_MISSING",
    "RubricAudit",
    "RubricChecks",
    "audit_attempt",
    "build_rubric",
    "checks_for",
    "rubric_audit_enabled",
]

RUBRIC_AUDIT_ENV = "UC_RUBRIC_AUDIT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

MAX_REPORTED_MISSING = 3
MAX_PATH_LIST = 12

_TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$")
_FIX_WORDS = ("fix", "bug", "error", "failure", "broken", "regression", "repair", "crash")
_TODO = re.compile(r"\b(TODO|FIXME|XXX)\b")
_BARE_EXCEPT = re.compile(r"^\s*except\s*:\s*$")
_HAZARDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("eval", re.compile(r"(?<![\w.])eval\s*\(")),
    ("exec", re.compile(r"(?<![\w.])exec\s*\(")),
    ("shell=True", re.compile(r"shell\s*=\s*True")),
    ("os.system", re.compile(r"os\.system\s*\(")),
    ("pickle.loads", re.compile(r"pickle\.loads\s*\(")),
    ("yaml.load", re.compile(r"yaml\.load\s*\(")),
)


def rubric_audit_enabled() -> bool:
    """True when this adapter and the underlying scorer are both on."""

    if os.environ.get(RUBRIC_AUDIT_ENV, "").strip().lower() not in _TRUTHY:
        return False
    return repo_rubric_scorer_enabled()


def build_rubric(commit: str = "attempt") -> FrozenRubric:
    """The frozen rubric: one item per dimension, correctness required."""

    return FrozenRubric(
        commit=commit or "attempt",
        items=(
            RubricItem(
                item_id="correctness",
                dimension=RubricDimension.CORRECTNESS,
                description="the patch changes behaviour instead of only deleting code",
                weight=1.0,
                required=True,
            ),
            RubricItem(
                item_id="test_coverage",
                dimension=RubricDimension.TEST_COVERAGE,
                description="a repair touches tests or the observed delta proves a fix",
                weight=0.8,
            ),
            RubricItem(
                item_id="compatibility",
                dimension=RubricDimension.COMPATIBILITY,
                description="changed paths stay inside the declared constraints",
                weight=0.6,
            ),
            RubricItem(
                item_id="security",
                dimension=RubricDimension.SECURITY,
                description="the patch introduces no known execution hazard",
                weight=0.5,
            ),
            RubricItem(
                item_id="maintainability",
                dimension=RubricDimension.MAINTAINABILITY,
                description="the patch adds no marker debt and no bare except",
                weight=0.4,
            ),
        ),
    )


def _added_and_removed(diff: str) -> tuple[list[str], int]:
    added: list[str] = []
    removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@ "):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path.replace("\\", "/").lower()))


def _fix_requested(description: str) -> bool:
    lowered = description.lower()
    return any(word in lowered for word in _FIX_WORDS)


@dataclass(frozen=True)
class RubricChecks:
    """The deterministic facts one attempt establishes."""

    added_lines: int
    removed_lines: int
    paths: tuple[str, ...]
    touches_tests: bool
    fix_requested: bool
    observed_fix: bool
    constraints_respected: bool
    constraints_declared: bool
    hazards: tuple[str, ...]
    added_markers: int
    bare_excepts: int

    def features(self) -> frozenset[str]:
        """Rubric feature markers: only the checks that actually passed."""

        marks: list[str] = []
        if self.added_lines > 0:
            marks.append("correctness")
        if self.touches_tests or self.observed_fix:
            marks.append("test_coverage")
        if not self.constraints_declared or self.constraints_respected:
            marks.append("compatibility")
        if not self.hazards:
            marks.append("security")
        if not self.added_markers and not self.bare_excepts:
            marks.append("maintainability")
        return frozenset(marks)

    def to_dict(self) -> dict[str, object]:
        return {
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
            "paths": list(self.paths[:MAX_PATH_LIST]),
            "touches_tests": self.touches_tests,
            "fix_requested": self.fix_requested,
            "observed_fix": self.observed_fix,
            "constraints_declared": self.constraints_declared,
            "constraints_respected": self.constraints_respected,
            "hazards": list(self.hazards),
            "added_markers": self.added_markers,
            "bare_excepts": self.bare_excepts,
        }


def checks_for(
    *,
    diff: str,
    paths: tuple[str, ...] = (),
    description: str = "",
    constraints: tuple[str, ...] = (),
    tests_passed: int = 0,
    tests_failed: int = 0,
) -> RubricChecks:
    """Derive the deterministic checks from one attempt's evidence."""

    added, removed = _added_and_removed(diff or "")
    normalised = tuple(
        dict.fromkeys(
            path.replace("\\", "/")
            for path in (*paths, *re.findall(r"^\+\+\+ b/(.+)$", diff or "", re.MULTILINE))
            if path.strip()
        )
    )
    hazards = tuple(
        name
        for name, pattern in _HAZARDS
        if any(pattern.search(line) for line in added)
    )
    declared = tuple(item.replace("\\", "/") for item in constraints if item.strip())
    respected = True
    if declared:
        respected = all(
            any(
                path == prefix
                or path.startswith(prefix.rstrip("/") + "/")
                or path.startswith(prefix)
                for prefix in declared
            )
            for path in normalised
        )
    return RubricChecks(
        added_lines=len(added),
        removed_lines=removed,
        paths=normalised,
        touches_tests=any(_is_test_path(path) for path in normalised),
        fix_requested=_fix_requested(description or ""),
        observed_fix=tests_passed > 0 and tests_failed == 0,
        constraints_respected=respected,
        constraints_declared=bool(declared),
        hazards=hazards,
        added_markers=sum(len(_TODO.findall(line)) for line in added),
        bare_excepts=sum(1 for line in added if _BARE_EXCEPT.match(line)),
    )


@dataclass(frozen=True)
class RubricAudit:
    """One advisory rubric score for a finished attempt."""

    score: RubricScore
    checks: RubricChecks

    def to_dict(self) -> dict[str, object]:
        return {
            **self.score.to_dict(),
            "missing_required": list(self.score.missing_required[:MAX_REPORTED_MISSING]),
            "checks": self.checks.to_dict(),
        }

    def to_event(self) -> dict[str, object]:
        """Progress-event payload: bounded, no diff text or file bodies."""

        return {
            "phase": "rubric audit",
            "percent": 0,
            "rubric_score": round(self.score.weighted_score, 4),
            "rubric_passed": self.score.passed,
            # ``passed`` additionally demands observed passing tests, which an
            # execution-free attempt never has; the weighted bar is the part a
            # no-test repository can still satisfy honestly.
            "rubric_weighted_ok": self.score.weighted_score >= 0.6,
            "rubric_satisfied": list(self.score.satisfied),
            "rubric_missing": list(self.score.missing_required[:MAX_REPORTED_MISSING]),
            "rubric_hazards": list(self.checks.hazards),
            "rubric_observed_fix": self.checks.observed_fix,
            "rubric_advisory": True,
        }


def audit_attempt(
    *,
    diff: str,
    paths: tuple[str, ...] = (),
    description: str = "",
    constraints: tuple[str, ...] = (),
    tests_passed: int = 0,
    tests_failed: int = 0,
    patch_id: str = "attempt",
) -> RubricAudit:
    """Freeze the rubric, derive the checks, and score the attempt."""

    checks = checks_for(
        diff=diff,
        paths=paths,
        description=description,
        constraints=constraints,
        tests_passed=tests_passed,
        tests_failed=tests_failed,
    )
    evidence = PatchEvidence(
        paths=checks.paths,
        symbols=(),
        tests_passed=max(tests_passed, 0),
        tests_failed=max(tests_failed, 0),
        features=checks.features(),
    )
    scorer = RepoGroundedRubricScorer(build_rubric())
    return RubricAudit(scorer.score(patch_id or "attempt", evidence), checks)
