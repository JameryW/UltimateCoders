"""Skill cards and four-stage curriculum validation (v10 #21).

The skill-curriculum upgrade turns reusable procedures into small, explicit
cards instead of a growing pile of prompt hints.  Every card has the same four
fields -- ``skill_id``, ``trigger``, ``procedure`` and ``verification`` -- and
must pass four increasingly expensive validation stages before it may be
ordered into a deployment curriculum:

* ``schema``    -- the card is structurally complete.
* ``replay``    -- the procedure reproduces a recorded successful trace.
* ``transfer``  -- the procedure still works on a different task/context.
* ``safety``    -- the procedure passed the safety review.

Validation is fail-fast and evidence-conditioned: a stage only passes when the
caller supplies concrete evidence for it, so an empty evidence mapping never
produces a green report.  ``curriculum_order`` then orders cards
deterministically by how far they were validated (most-validated first), then
by caller priority, then by ``skill_id``.

Everything here is opt-in and pure; the feature flag
:func:`skill_curriculum_enabled` reads ``UC_SKILL_CURRICULUM``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "STAGES",
    "SKILL_CURRICULUM_ENV",
    "SkillCard",
    "SkillCurriculum",
    "StageResult",
    "ValidationReport",
    "curriculum_order",
    "four_stage_validate",
    "skill_curriculum_enabled",
]

#: Validation stages in the order they are evaluated.
STAGES = ("schema", "replay", "transfer", "safety")
#: Environment flag that enables the skill curriculum.
SKILL_CURRICULUM_ENV = "UC_SKILL_CURRICULUM"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_EVIDENCE_FALSE = frozenset({"", "0", "false", "no", "fail", "failed", "none", "skip", "skipped"})


def skill_curriculum_enabled() -> bool:
    """Return whether skill cards/curriculum are enabled via ``UC_SKILL_CURRICULUM``."""

    return os.environ.get(SKILL_CURRICULUM_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class SkillCard:
    """One reusable procedure with its trigger and verification recipe."""

    skill_id: str
    trigger: str
    procedure: str
    verification: str

    def __post_init__(self) -> None:
        for field_name in ("skill_id", "trigger", "procedure", "verification"):
            object.__setattr__(self, field_name, str(getattr(self, field_name) or ""))

    def missing_fields(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in ("skill_id", "trigger", "procedure", "verification")
            if not getattr(self, name).strip()
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "skill_id": self.skill_id,
            "trigger": self.trigger,
            "procedure": self.procedure,
            "verification": self.verification,
        }


@dataclass(frozen=True)
class StageResult:
    """One validation stage outcome."""

    stage: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"stage": self.stage, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ValidationReport:
    """Outcome of :func:`four_stage_validate` for one card.

    ``failed_stage`` is the first stage that failed; later stages are marked
    ``passed=False`` with a "not evaluated" detail.  ``stage_index`` is the
    number of consecutive passed stages (0..4), which is what
    :func:`curriculum_order` uses for ranking.
    """

    card_id: str
    stages: tuple[StageResult, ...]
    failed_stage: str | None = None

    @property
    def passed(self) -> bool:
        return self.failed_stage is None and all(result.passed for result in self.stages)

    @property
    def stage_index(self) -> int:
        count = 0
        for result in self.stages:
            if not result.passed:
                break
            count += 1
        return count

    @property
    def stage(self) -> str:
        """Name of the last passed stage, or ``"none"``."""

        index = self.stage_index
        return self.stages[index - 1].stage if index else "none"

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "stages": [result.to_dict() for result in self.stages],
            "failed_stage": self.failed_stage,
            "passed": self.passed,
            "stage": self.stage,
        }


def four_stage_validate(card: SkillCard, evidence: Mapping[str, Any] | None) -> ValidationReport:
    """Validate ``card`` through ``schema`` -> ``replay`` -> ``transfer`` -> ``safety``.

    ``evidence`` maps stage names to concrete evidence.  Accepted values are
    booleans, non-empty collections of evidence ids, non-empty strings, or a
    mapping carrying ``passed``/``ok``/``success``/``valid``.  Missing or
    falsey evidence fails the stage (fail-closed).  The schema stage also
    checks the four card fields directly; the caller's ``schema`` evidence can
    only add a constraint, never waive a missing field.
    """

    if not isinstance(card, SkillCard):
        raise TypeError("card must be a SkillCard")
    supplied = evidence if isinstance(evidence, Mapping) else {}

    results: list[StageResult] = []
    failed_stage: str | None = None
    for stage in STAGES:
        if failed_stage is not None:
            results.append(StageResult(stage=stage, passed=False, detail="not evaluated"))
            continue

        if stage == "schema":
            missing = card.missing_fields()
            if missing:
                failed_stage = stage
                results.append(
                    StageResult(
                        stage=stage,
                        passed=False,
                        detail=f"missing required fields: {', '.join(missing)}",
                    )
                )
                continue
            if "schema" in supplied and not _evidence_passed(supplied["schema"]):
                failed_stage = stage
                results.append(
                    StageResult(stage=stage, passed=False, detail="schema evidence failed")
                )
                continue
            results.append(StageResult(stage=stage, passed=True, detail="card schema complete"))
            continue

        value = supplied.get(stage)
        if _evidence_passed(value):
            results.append(
                StageResult(stage=stage, passed=True, detail=f"{stage} evidence accepted")
            )
        else:
            failed_stage = stage
            results.append(
                StageResult(stage=stage, passed=False, detail=f"missing or failed {stage} evidence")
            )

    return ValidationReport(
        card_id=card.skill_id,
        stages=tuple(results),
        failed_stage=failed_stage,
    )


class SkillCurriculum:
    """An in-memory set of skill cards with deterministic ordering."""

    def __init__(self, cards: Iterable[SkillCard] = ()) -> None:
        self._cards: dict[str, SkillCard] = {}
        self._priorities: dict[str, int] = {}
        self._reports: dict[str, ValidationReport] = {}
        for card in cards:
            self.add(card)

    def __len__(self) -> int:
        return len(self._cards)

    def __iter__(self):
        return iter(self._cards.values())

    def add(self, card: SkillCard, *, priority: int = 0) -> SkillCard:
        """Add one card; duplicate ``skill_id`` values are rejected."""

        if not isinstance(card, SkillCard):
            raise TypeError("card must be a SkillCard")
        if card.skill_id in self._cards:
            raise ValueError(f"duplicate skill id: {card.skill_id!r}")
        self._cards[card.skill_id] = card
        self._priorities[card.skill_id] = int(priority)
        return card

    def get(self, skill_id: str) -> SkillCard | None:
        return self._cards.get(str(skill_id))

    def validate(
        self, card: SkillCard | str, evidence: Mapping[str, Any] | None
    ) -> ValidationReport:
        """Validate a card (or a known id) and remember the latest report."""

        target = self._cards[str(card)] if isinstance(card, str) else card
        report = four_stage_validate(target, evidence)
        self._reports[report.card_id] = report
        return report

    def curriculum_order(
        self,
        reports: Mapping[str, ValidationReport] | None = None,
    ) -> tuple[SkillCard, ...]:
        """Return cards ordered by validated stage, priority, then id.

        Stage rank is descending: a card validated through ``safety`` comes
        before one that only passed ``schema``.  Ties are broken by higher
        caller priority first and ``skill_id`` ascending for determinism.
        Reports recorded through :meth:`validate` are used when ``reports`` is
        omitted.
        """

        chosen = self._reports if reports is None else reports
        return curriculum_order(
            self._cards.values(),
            chosen,
            priorities=self._priorities,
        )


def curriculum_order(
    cards: Iterable[SkillCard],
    reports: Mapping[str, ValidationReport] | None = None,
    *,
    priorities: Mapping[str, int] | None = None,
) -> tuple[SkillCard, ...]:
    """Deterministically order cards by validation stage and priority.

    This module-level form is useful when a caller keeps cards and reports
    elsewhere; :meth:`SkillCurriculum.curriculum_order` delegates here.
    """

    report_map = reports or {}
    priority_map = priorities or {}

    def sort_key(card: SkillCard) -> tuple[int, int, str]:
        report = report_map.get(card.skill_id)
        stage_index = report.stage_index if report is not None else 0
        return (-stage_index, -int(priority_map.get(card.skill_id, 0)), card.skill_id)

    return tuple(sorted(cards, key=sort_key))


def _evidence_passed(value: Any) -> bool:
    """Interpret one stage's evidence value truthfully but defensively."""

    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, Mapping):
        for key in ("passed", "ok", "success", "valid"):
            if key in value:
                return _evidence_passed(value[key])
        if "status" in value:
            return _evidence_passed(value["status"])
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _EVIDENCE_FALSE
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_evidence_passed(item) for item in value)
    return bool(value)
