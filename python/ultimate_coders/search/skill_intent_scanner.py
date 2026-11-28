"""Hybrid malicious-intent auditing for third-party agent skills (ledger item #101).

Static rules, sandbox dynamic evidence, and an optional LLM review are fused
into a risk score. The Tasklet ledger maps each generated line back to the
planning/build prompt that produced it and exports a compact ADR.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "SKILL_INTENT_SCANNER_ENV",
    "EvidenceKind",
    "SkillEvidence",
    "SkillFinding",
    "SkillScanReport",
    "SkillIntentScanner",
    "Tasklet",
    "TaskletLedger",
    "skill_intent_scanner_enabled",
]

SKILL_INTENT_SCANNER_ENV = "UC_SKILL_INTENT_SCANNER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_STATIC_RULES: tuple[tuple[str, str, float], ...] = (
    ("prompt_override", r"ignore (all )?(previous|prior) instructions", 0.9),
    ("credential_access", r"(\.ssh|\.aws|id_rsa|credentials|secrets?)", 0.5),
    ("network_exfil", r"(curl|wget|requests\.post|fetch\()\s+https?://", 0.7),
    ("destructive", r"(rm\s+-rf|drop\s+table|format\s+[a-z]:)", 0.9),
    ("encoded_payload", r"(base64\s+-d|eval\(|exec\()", 0.6),
    ("privilege_escalation", r"(sudo|chmod\s+777|setuid)", 0.8),
)


def skill_intent_scanner_enabled() -> bool:
    return os.environ.get(SKILL_INTENT_SCANNER_ENV, "").strip().lower() in _TRUTHY


class EvidenceKind(str, Enum):
    STATIC = "static"
    DYNAMIC = "dynamic"
    LLM = "llm"


@dataclass(frozen=True)
class SkillEvidence:
    kind: EvidenceKind
    signal: str
    weight: float

    def __post_init__(self) -> None:
        if not self.signal.strip():
            raise ValueError("signal must not be empty")
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError("weight must be in [0, 1]")


@dataclass(frozen=True)
class SkillFinding:
    rule: str
    severity: str
    evidence_kind: EvidenceKind
    detail: str


@dataclass(frozen=True)
class SkillScanReport:
    skill: str
    score: float
    malicious: bool
    findings: tuple[SkillFinding, ...]

    @property
    def precision_proxy(self) -> float:
        if not self.findings:
            return 0.0
        high = sum(1 for finding in self.findings if finding.severity == "high")
        return high / len(self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "skill": self.skill,
            "score": self.score,
            "malicious": self.malicious,
            "findings": [
                {
                    "rule": finding.rule,
                    "severity": finding.severity,
                    "evidence_kind": finding.evidence_kind.value,
                    "detail": finding.detail,
                }
                for finding in self.findings
            ],
        }


@dataclass
class SkillIntentScanner:
    threshold: float = 0.6

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be in [0, 1]")

    def scan(
        self,
        skill: str,
        instructions: str,
        *,
        dynamic_signals: tuple[str, ...] = (),
        llm_signals: tuple[str, ...] = (),
    ) -> SkillScanReport:
        if not skill.strip():
            raise ValueError("skill must not be empty")
        findings: list[SkillFinding] = []
        score = 0.0
        for rule, pattern, weight in _STATIC_RULES:
            if re.search(pattern, instructions, re.IGNORECASE):
                findings.append(
                    SkillFinding(
                        rule,
                        "high" if weight >= 0.7 else "medium",
                        EvidenceKind.STATIC,
                        pattern,
                    )
                )
                score = max(score, weight)
        for signal in dynamic_signals:
            if signal.strip():
                findings.append(
                    SkillFinding("dynamic_signal", "high", EvidenceKind.DYNAMIC, signal)
                )
                score = min(1.0, score + 0.15)
        for signal in llm_signals:
            if signal.strip():
                findings.append(SkillFinding("llm_review", "medium", EvidenceKind.LLM, signal))
                score = min(1.0, score + 0.1)
        return SkillScanReport(skill, score, score >= self.threshold, tuple(findings))


@dataclass(frozen=True)
class Tasklet:
    tasklet_id: str
    path: str
    line: int
    prompt: str
    build_prompt: str

    def to_dict(self) -> dict[str, object]:
        return {
            "tasklet_id": self.tasklet_id,
            "path": self.path,
            "line": self.line,
            "prompt": self.prompt,
            "build_prompt": self.build_prompt,
        }


@dataclass
class TaskletLedger:
    _tasklets: dict[tuple[str, int], Tasklet] = field(default_factory=dict)
    _next_id: int = 1

    def record(self, path: str, line: int, prompt: str, build_prompt: str) -> Tasklet:
        if not path.strip() or line <= 0 or not prompt.strip() or not build_prompt.strip():
            raise ValueError("path, positive line, prompt, and build_prompt are required")
        tasklet = Tasklet(f"tasklet-{self._next_id}", path, line, prompt, build_prompt)
        self._next_id += 1
        self._tasklets[(path, line)] = tasklet
        return tasklet

    def lineage(self, path: str, line: int) -> Tasklet | None:
        return self._tasklets.get((path, line))

    def export_adr(self) -> str:
        lines = ["# Line-level Tasklet ADR", ""]
        for (path, line), tasklet in sorted(self._tasklets.items()):
            lines.append(f"- `{path}:{line}` ← {tasklet.prompt} (build: {tasklet.build_prompt})")
        return "\n".join(lines)
