"""EvoGroup shared-context harness evolution (ledger item #82).

Trajectory selection, low-confidence reflection, progressive disclosure and a
small evolve-agent loop turn a six-tool cold-start harness into a shared,
versioned group harness. All state is local and deterministic.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "EVOGROUP_HARNESS_ENV",
    "EvoGroupError",
    "ToolSpec",
    "GroupTrace",
    "TraceSelection",
    "Bucket",
    "DisclosureReport",
    "HarnessMutation",
    "SharedHarness",
    "EvoGroupHarness",
    "evogroup_harness_enabled",
]

EVOGROUP_HARNESS_ENV = "UC_EVOGROUP_HARNESS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def evogroup_harness_enabled() -> bool:
    return os.environ.get(EVOGROUP_HARNESS_ENV, "").strip().lower() in _TRUTHY


class EvoGroupError(ValueError):
    """Raised when a group harness update is malformed."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise EvoGroupError("tool name must not be empty")


DEFAULT_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("read", "read a file"),
    ToolSpec("write", "write a file"),
    ToolSpec("glob", "list files"),
    ToolSpec("grep", "search text"),
    ToolSpec("test", "run tests"),
    ToolSpec("done", "finish the task"),
)


@dataclass(frozen=True)
class GroupTrace:
    trace_id: str
    confidence: float
    success: bool
    steps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.trace_id.strip():
            raise EvoGroupError("trace id must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise EvoGroupError("confidence must be in [0, 1]")
        object.__setattr__(self, "steps", tuple(str(step) for step in self.steps))


@dataclass(frozen=True)
class TraceSelection:
    kept: tuple[GroupTrace, ...]
    rejected: tuple[GroupTrace, ...]
    reflect: tuple[GroupTrace, ...]

    @property
    def kept_count(self) -> int:
        return len(self.kept)


class TrajectorySelector:
    """Keep informative traces and flag low-confidence ones for reflection."""

    def __init__(self, low_confidence: float = 0.4, high_confidence: float = 0.95) -> None:
        if not 0.0 <= low_confidence < high_confidence <= 1.0:
            raise EvoGroupError("invalid confidence band")
        self.low_confidence = low_confidence
        self.high_confidence = high_confidence

    def select(self, traces: Iterable[GroupTrace]) -> TraceSelection:
        kept: list[GroupTrace] = []
        rejected: list[GroupTrace] = []
        reflect: list[GroupTrace] = []
        for trace in traces:
            if trace.confidence < self.low_confidence:
                reflect.append(trace)
            elif trace.confidence > self.high_confidence and not trace.success:
                rejected.append(trace)
            else:
                kept.append(trace)
        return TraceSelection(tuple(kept), tuple(rejected), tuple(reflect))


@dataclass(frozen=True)
class Bucket:
    phase: str
    total: int
    successes: int
    mean_confidence: float

    @property
    def success_rate(self) -> float:
        return self.successes / self.total if self.total else 0.0


@dataclass(frozen=True)
class DisclosureReport:
    buckets: tuple[Bucket, ...]
    recommended_level: str
    reflected: int

    def to_dict(self) -> dict[str, object]:
        return {
            "buckets": [
                {
                    "phase": bucket.phase,
                    "total": bucket.total,
                    "success_rate": bucket.success_rate,
                    "mean_confidence": bucket.mean_confidence,
                }
                for bucket in self.buckets
            ],
            "recommended_level": self.recommended_level,
            "reflected": self.reflected,
        }


class ProgressiveDisclosureAnalyzer:
    """Aggregate traces by phase and choose a disclosure depth."""

    def analyze(self, traces: Sequence[GroupTrace], selection: TraceSelection) -> DisclosureReport:
        phases: dict[str, list[GroupTrace]] = {}
        for trace in traces:
            phase = trace.steps[0] if trace.steps else "unknown"
            phases.setdefault(phase, []).append(trace)
        buckets = tuple(
            Bucket(
                phase=phase,
                total=len(items),
                successes=sum(1 for item in items if item.success),
                mean_confidence=sum(item.confidence for item in items) / len(items),
            )
            for phase, items in sorted(phases.items())
        )
        if not buckets:
            return DisclosureReport((), "summary", len(selection.reflect))
        if len(buckets) <= 2:
            level = "full"
        elif len(buckets) <= 4:
            level = "bucketed"
        else:
            level = "summary"
        return DisclosureReport(buckets, level, len(selection.reflect))


@dataclass(frozen=True)
class HarnessMutation:
    section: str
    instruction: str
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.section.strip() or not self.instruction.strip():
            raise EvoGroupError("mutation section and instruction are required")
        object.__setattr__(self, "evidence", tuple(str(item) for item in self.evidence))


@dataclass
class SharedHarness:
    tools: tuple[ToolSpec, ...]
    instructions: tuple[str, ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        if not self.tools:
            raise EvoGroupError("shared harness requires at least one tool")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise EvoGroupError("duplicate tool names")

    def apply(self, mutation: HarnessMutation) -> None:
        if mutation.section == "tools":
            raise EvoGroupError("tool mutation is not supported by this harness")
        self.instructions = (*self.instructions, mutation.instruction)
        self.version += 1

    def to_dict(self) -> dict[str, object]:
        return {
            "tools": [tool.name for tool in self.tools],
            "instructions": list(self.instructions),
            "version": self.version,
        }


class EvoGroupHarness:
    """Three-phase shared-context evolution pipeline."""

    def __init__(
        self,
        tools: Sequence[ToolSpec] = DEFAULT_TOOLS,
        *,
        low_confidence: float = 0.4,
        high_confidence: float = 0.95,
    ) -> None:
        self._traces: list[GroupTrace] = []
        self._harness = SharedHarness(tuple(tools))
        self._selector = TrajectorySelector(low_confidence, high_confidence)
        self._analyzer = ProgressiveDisclosureAnalyzer()

    @property
    def harness(self) -> SharedHarness:
        return self._harness

    @property
    def traces(self) -> tuple[GroupTrace, ...]:
        return tuple(self._traces)

    def record(self, trace: GroupTrace) -> None:
        self._traces.append(trace)

    def evolve(self) -> tuple[TraceSelection, DisclosureReport, SharedHarness]:
        selection = self._selector.select(self._traces)
        report = self._analyzer.analyze(self._traces, selection)
        if selection.reflect:
            self._harness.apply(
                HarnessMutation(
                    "instructions",
                    "When confidence is low, inspect the current workspace before acting.",
                    tuple(trace.trace_id for trace in selection.reflect[:4]),
                )
            )
        if report.recommended_level == "summary":
            self._harness.apply(
                HarnessMutation(
                    "instructions",
                    "Summarize phase state before starting a new phase.",
                    tuple(bucket.phase for bucket in report.buckets[:4]),
                )
            )
        return selection, report, self._harness

    def reset(self) -> None:
        self._traces.clear()
        self._harness = SharedHarness(self._harness.tools)
