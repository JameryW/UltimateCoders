"""Zep-style verified-trace skill compiler (ledger item #111, UC_ZEP_SKILL_COMPILER).

Reflector extracts reusable skills only from verified successful traces;
Curator admits them after a safety scan and ranks them by verified use.
No model or network call is made.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

ZEP_SKILL_COMPILER_ENV = "UC_ZEP_SKILL_COMPILER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_UNSAFE = ("rm -rf", "curl | sh", "os.system", "subprocess.call")


def zep_skill_compiler_enabled() -> bool:
    return os.environ.get(ZEP_SKILL_COMPILER_ENV, "").strip().lower() in _TRUTHY


class ZepSkillError(ValueError):
    """Raised for malformed traces or skills."""


@dataclass(frozen=True)
class TraceEvent:
    step: int
    action: str
    target: str

    def __post_init__(self) -> None:
        if self.step < 0 or not self.action.strip():
            raise ZepSkillError("invalid trace event")


@dataclass(frozen=True)
class SkillTrace:
    trace_id: str
    task: str
    model: str
    verified: bool
    success: bool
    events: tuple[TraceEvent, ...]

    def __post_init__(self) -> None:
        if not self.trace_id.strip() or not self.task.strip():
            raise ZepSkillError("trace id and task must not be empty")
        if not self.events:
            raise ZepSkillError("trace must contain events")


@dataclass(frozen=True)
class SkillEvidence:
    trace_id: str
    step: int
    action: str


@dataclass
class CompiledSkill:
    name: str
    summary: str
    steps: tuple[str, ...]
    evidence: tuple[SkillEvidence, ...] = ()
    uses: int = 0
    wins: int = 0

    @property
    def win_rate(self) -> float:
        return 0.0 if self.uses == 0 else round(self.wins / self.uses, 4)


@dataclass(frozen=True)
class SkillCompileReport:
    compiled: tuple[CompiledSkill, ...]
    rejected_traces: int
    admitted: int

    def to_dict(self) -> dict[str, object]:
        return {
            "compiled": [skill.name for skill in self.compiled],
            "rejected_traces": self.rejected_traces,
            "admitted": self.admitted,
        }


class Reflector:
    def compile(
        self, traces: tuple[SkillTrace, ...] | list[SkillTrace]
    ) -> tuple[CompiledSkill, ...]:
        grouped: dict[str, list[SkillTrace]] = {}
        rejected = 0
        for trace in traces:
            if not trace.verified or not trace.success:
                rejected += 1
                continue
            grouped.setdefault(trace.task, []).append(trace)
        compiled: list[CompiledSkill] = []
        for task in sorted(grouped):
            group = grouped[task]
            if len(group) < 2:
                rejected += 1
                continue
            sequences = [tuple(event.action for event in trace.events) for trace in group]
            if len(set(sequences)) != 1:
                rejected += 1
                continue
            evidence = tuple(
                SkillEvidence(trace.trace_id, event.step, event.action)
                for trace in group
                for event in trace.events
            )
            name = "skill:" + re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")
            compiled.append(
                CompiledSkill(
                    name=name,
                    summary=f"Verified workflow for {task}",
                    steps=sequences[0],
                    evidence=evidence,
                )
            )
        return tuple(compiled)


class Curator:
    def __init__(self) -> None:
        self._skills: dict[str, CompiledSkill] = {}

    def admit(self, skill: CompiledSkill) -> bool:
        if not skill.name.strip() or not skill.summary.strip() or not skill.steps:
            raise ZepSkillError("skill must have name, summary and steps")
        lowered = " ".join(skill.steps).lower()
        if any(pattern in lowered for pattern in _UNSAFE):
            return False
        self._skills[skill.name] = skill
        return True

    def record_use(self, name: str, success: bool) -> None:
        if name not in self._skills:
            raise ZepSkillError(f"unknown skill: {name}")
        skill = self._skills[name]
        skill.uses += 1
        if success:
            skill.wins += 1

    def rank(self) -> tuple[CompiledSkill, ...]:
        return tuple(
            sorted(
                self._skills.values(),
                key=lambda skill: (-skill.win_rate, -skill.uses, skill.name),
            )
        )

    def expose(self, max_chars: int = 1200) -> str:
        if max_chars < 1:
            raise ZepSkillError("max_chars must be positive")
        lines = ["## Verified skills"]
        budget = max_chars - len(lines[0])
        for skill in self.rank():
            line = f"- {skill.name}: {skill.summary} [steps: {' -> '.join(skill.steps)}]"
            if len(line) > budget:
                break
            lines.append(line)
            budget -= len(line) + 1
        return "\n".join(lines)


class ZepSkillCompiler:
    def __init__(self) -> None:
        self.reflector = Reflector()
        self.curator = Curator()

    def compile(self, traces: tuple[SkillTrace, ...] | list[SkillTrace]) -> SkillCompileReport:
        compiled = self.reflector.compile(traces)
        admitted = sum(1 for skill in compiled if self.curator.admit(skill))
        rejected = len(traces) - sum(1 for trace in traces if trace.verified and trace.success)
        return SkillCompileReport(compiled=compiled, rejected_traces=rejected, admitted=admitted)


__all__ = [
    "ZEP_SKILL_COMPILER_ENV",
    "CompiledSkill",
    "Curator",
    "Reflector",
    "SkillCompileReport",
    "SkillEvidence",
    "SkillTrace",
    "TraceEvent",
    "ZepSkillCompiler",
    "ZepSkillError",
    "zep_skill_compiler_enabled",
]
