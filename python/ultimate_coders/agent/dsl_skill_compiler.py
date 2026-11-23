"""Enterprise DSL compile-edit loop and skill wording distillation (ledger item #87).

AgentScript-Eval showed that enterprise DSLs have near-zero zero-shot creation
accuracy and that a compile-edit feedback loop can double partial credit. This
module keeps the loop deterministic: callers supply a compiler callback and an
optional editor callback, while the module records every attempt and chooses
the best evidence. A separate skill-pack evaluator compares wording variants.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "DSL_SKILL_COMPILER_ENV",
    "DslSkillError",
    "SkillFormat",
    "CompileResult",
    "CompileAttempt",
    "CompileLoopReport",
    "DslTask",
    "DslSkillPack",
    "SkillVariant",
    "SkillDistillation",
    "DslSkillCompiler",
    "dsl_skill_compiler_enabled",
]

DSL_SKILL_COMPILER_ENV = "UC_DSL_SKILL_COMPILER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def dsl_skill_compiler_enabled() -> bool:
    return os.environ.get(DSL_SKILL_COMPILER_ENV, "").strip().lower() in _TRUTHY


class DslSkillError(ValueError):
    """Raised when a DSL task or skill pack is malformed."""


class SkillFormat(str, Enum):
    PLUGIN = "plugin"
    DOC = "doc"


@dataclass(frozen=True)
class CompileResult:
    passed: bool
    partial_score: float
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.partial_score <= 1.0:
            raise DslSkillError("partial score must be in [0, 1]")
        if not self.passed and not self.errors:
            raise DslSkillError("a failed compile must report at least one error")


@dataclass(frozen=True)
class CompileAttempt:
    round_index: int
    source: str
    result: CompileResult

    def to_dict(self) -> dict[str, object]:
        return {
            "round": self.round_index,
            "source": self.source,
            "passed": self.result.passed,
            "partial_score": self.result.partial_score,
            "errors": list(self.result.errors),
        }


@dataclass(frozen=True)
class CompileLoopReport:
    task_id: str
    success: bool
    rounds: int
    best_score: float
    best_source: str
    attempts: tuple[CompileAttempt, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "success": self.success,
            "rounds": self.rounds,
            "best_score": self.best_score,
            "best_source": self.best_source,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


@dataclass(frozen=True)
class DslTask:
    task_id: str
    description: str
    requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise DslSkillError("task id must not be empty")
        if not self.description.strip():
            raise DslSkillError("task description must not be empty")


@dataclass(frozen=True)
class DslSkillPack:
    name: str
    format: SkillFormat
    instructions: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise DslSkillError("skill pack name must not be empty")
        if not self.instructions.strip():
            raise DslSkillError("skill pack instructions must not be empty")


@dataclass(frozen=True)
class SkillVariant:
    name: str
    text: str
    score: float

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise DslSkillError("variant name must not be empty")
        if not self.text.strip():
            raise DslSkillError("variant text must not be empty")
        if not 0.0 <= self.score <= 1.0:
            raise DslSkillError("variant score must be in [0, 1]")


@dataclass(frozen=True)
class SkillDistillation:
    winner: SkillVariant
    baseline_score: float
    delta: float

    def to_dict(self) -> dict[str, object]:
        return {
            "winner": self.winner.name,
            "score": self.winner.score,
            "baseline": self.baseline_score,
            "delta": self.delta,
        }


@dataclass
class DslSkillCompiler:
    """Runs a bounded compile-edit loop and distils skill wording."""

    max_rounds: int = 3
    _attempts: list[CompileAttempt] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.max_rounds <= 0:
            raise DslSkillError("max_rounds must be positive")

    @property
    def attempts(self) -> tuple[CompileAttempt, ...]:
        return tuple(self._attempts)

    def compile_edit(
        self,
        task: DslTask,
        initial_source: str,
        compiler: Callable[[str], CompileResult],
        editor: Callable[[str, CompileResult], str] | None = None,
    ) -> CompileLoopReport:
        if not isinstance(task, DslTask):
            raise DslSkillError("task must be a DslTask")
        if not initial_source.strip():
            raise DslSkillError("initial source must not be empty")
        self._attempts.clear()
        source = initial_source
        best_result = CompileResult(False, 0.0, ("not compiled",))
        best_source = source
        for round_index in range(1, self.max_rounds + 1):
            result = compiler(source)
            if not isinstance(result, CompileResult):
                raise DslSkillError("compiler must return CompileResult")
            self._attempts.append(CompileAttempt(round_index, source, result))
            if result.partial_score >= best_result.partial_score:
                best_result = result
                best_source = source
            if result.passed:
                return CompileLoopReport(
                    task.task_id,
                    True,
                    round_index,
                    result.partial_score,
                    source,
                    tuple(self._attempts),
                )
            if editor is None:
                break
            source = editor(source, result)
            if not source.strip():
                raise DslSkillError("editor returned an empty source")
        return CompileLoopReport(
            task.task_id,
            False,
            len(self._attempts),
            best_result.partial_score,
            best_source,
            tuple(self._attempts),
        )

    def distill_skill(
        self,
        variants: Iterable[SkillVariant],
        *,
        baseline_name: str | None = None,
    ) -> SkillDistillation:
        values = tuple(variants)
        if not values:
            raise DslSkillError("at least one skill variant is required")
        if len({item.name for item in values}) != len(values):
            raise DslSkillError("variant names must be unique")
        winner = max(values, key=lambda item: (item.score, item.name))
        if baseline_name is None:
            baseline = min(item.score for item in values)
        else:
            match = next((item for item in values if item.name == baseline_name), None)
            if match is None:
                raise DslSkillError(f"unknown baseline variant: {baseline_name}")
            baseline = match.score
        return SkillDistillation(winner, baseline, round(winner.score - baseline, 4))

    def reformat(self, pack: DslSkillPack, target: SkillFormat) -> DslSkillPack:
        if not isinstance(pack, DslSkillPack):
            raise DslSkillError("pack must be a DslSkillPack")
        if not isinstance(target, SkillFormat):
            raise DslSkillError("target must be a SkillFormat")
        if target == pack.format:
            return pack
        return DslSkillPack(pack.name, target, pack.instructions)
