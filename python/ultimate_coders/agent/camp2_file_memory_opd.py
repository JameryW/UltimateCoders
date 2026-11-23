"""CAMP-II (#44): native file memory and memory-write OPD supervision.

The ledger's CAMP-II result is that dedicated memory *tools* lose to the file
operations a coding model already knows from pretraining. The memory layer is
therefore a pair of ordinary workspace files:

* ``.uc_memory/notes.md`` — durable task notes, decisions, and evidence.
* ``.uc_memory/continuation.md`` — the next-action handoff across context
  replacement.

The second half of the module models the fully asynchronous online policy
distillation (OPD) signal. Teacher and student log-probabilities are compared
only on tokens that actually wrote memory, so the training signal targets the
behaviour the ledger wants to improve instead of the entire trajectory.

Opt-in flag: ``UC_CAMP2_FILE_MEMORY_OPD``. Pure bookkeeping, no file I/O.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

__all__ = [
    "ALLOWED_MEMORY_PATHS",
    "CAMP2_FILE_MEMORY_OPD_ENV",
    "CONTINUATION_PATH",
    "MEMORY_ROOT",
    "NOTES_PATH",
    "Camp2OpdDistiller",
    "FileMemoryError",
    "FileMemoryPrior",
    "FileMemorySession",
    "FileMemoryWrite",
    "MemoryFile",
    "MemoryWriteAdvantage",
    "MemoryWriteSample",
    "OpdReport",
    "TokenLogprobError",
    "camp2_file_memory_opd_enabled",
    "validate_memory_path",
]

CAMP2_FILE_MEMORY_OPD_ENV = "UC_CAMP2_FILE_MEMORY_OPD"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

MEMORY_ROOT = ".uc_memory"
NOTES_PATH = f"{MEMORY_ROOT}/notes.md"
CONTINUATION_PATH = f"{MEMORY_ROOT}/continuation.md"
ALLOWED_MEMORY_PATHS = (NOTES_PATH, CONTINUATION_PATH)


def camp2_file_memory_opd_enabled() -> bool:
    """Return whether the CAMP-II file-memory path is enabled."""

    return os.environ.get(CAMP2_FILE_MEMORY_OPD_ENV, "").strip().lower() in _TRUTHY


class FileMemoryError(ValueError):
    """Raised when a file-memory path or write is malformed."""


class TokenLogprobError(ValueError):
    """Raised when an OPD token sample is internally inconsistent."""


def validate_memory_path(path: str) -> str:
    """Validate and normalize one of the two native memory paths."""

    candidate = path.strip().replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if candidate not in ALLOWED_MEMORY_PATHS:
        raise FileMemoryError(
            f"memory path must be one of {ALLOWED_MEMORY_PATHS}, got {path!r}"
        )
    return candidate


@dataclass(frozen=True)
class FileMemoryWrite:
    """One bounded write to a native memory file."""

    path: str
    content: str
    step: int = 0
    next_action: str = ""
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", validate_memory_path(self.path))
        if not self.content.strip():
            raise FileMemoryError("memory write content must not be empty")
        if self.step < 0:
            raise FileMemoryError("memory write step must be non-negative")
        if self.path == CONTINUATION_PATH and not self.next_action.strip():
            raise FileMemoryError("continuation writes require a next_action")
        object.__setattr__(self, "evidence", tuple(str(item) for item in self.evidence))

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "content": self.content,
            "step": self.step,
            "next_action": self.next_action,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class MemoryFile:
    """The current contents of one native memory file."""

    path: str
    content: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", validate_memory_path(self.path))


@dataclass(frozen=True)
class FileMemoryPrior:
    """Rendering, appending, and prompting rules for native file memory."""

    max_chars: int = 6000

    def __post_init__(self) -> None:
        if self.max_chars < 80:
            raise FileMemoryError("max_chars must be at least 80")

    def render_write(self, write: FileMemoryWrite) -> str:
        """Render one write as a compact Markdown section."""

        lines = [f"## Step {write.step} — {write.path}", "", write.content.strip()]
        if write.evidence:
            lines.extend(["", "Evidence:", *[f"- {item}" for item in write.evidence]])
        if write.next_action.strip():
            lines.extend(["", f"Next: {write.next_action.strip()}"])
        return "\n".join(lines).strip()

    def append(self, existing: MemoryFile, write: FileMemoryWrite) -> MemoryFile:
        """Append one write while keeping only the most recent tail."""

        if existing.path != write.path:
            raise FileMemoryError("cannot append a write to a different memory file")
        section = self.render_write(write)
        if existing.content.strip():
            body = f"{existing.content.strip()}\n\n{section}".strip()
        else:
            body = section
        if len(body) > self.max_chars:
            keep = self.max_chars - len("… (truncated)\n")
            body = f"… (truncated)\n{body[-keep:]}"
        return MemoryFile(path=write.path, content=body)

    def system_prompt(self) -> str:
        """The native-interface instruction used instead of a memory tool."""

        return (
            "Maintain continuity with ordinary workspace files, not a memory tool. "
            f"Keep durable notes in {NOTES_PATH} and the next-action handoff in "
            f"{CONTINUATION_PATH}. Write only decisions, evidence, and the immediate "
            "next action; do not paste whole logs."
        )


class FileMemorySession:
    """Small in-memory model of a two-file native memory workspace."""

    def __init__(self, prior: FileMemoryPrior | None = None) -> None:
        self.prior = prior or FileMemoryPrior()
        self._files: dict[str, str] = {NOTES_PATH: "", CONTINUATION_PATH: ""}

    def write(self, write: FileMemoryWrite) -> MemoryFile:
        current = MemoryFile(path=write.path, content=self._files[write.path])
        updated = self.prior.append(current, write)
        self._files[write.path] = updated.content
        return updated

    def read(self, path: str) -> MemoryFile:
        normalized = validate_memory_path(path)
        return MemoryFile(path=normalized, content=self._files[normalized])

    def render_context(self, max_chars: int = 2400) -> str:
        """Render both files for prompt injection, newest content first."""

        blocks = []
        for path in (CONTINUATION_PATH, NOTES_PATH):
            content = self._files[path].strip()
            if content:
                blocks.append(f"### {path}\n{content}")
        text = "\n\n".join(blocks)
        if len(text) > max_chars:
            text = f"… (truncated)\n{text[-max_chars:]}"
        return text

    def files(self) -> dict[str, str]:
        return dict(self._files)


@dataclass(frozen=True)
class MemoryWriteSample:
    """One teacher/student token sample aligned with a memory write."""

    step: int
    token_ids: tuple[int, ...]
    teacher_logprobs: tuple[float, ...]
    student_logprobs: tuple[float, ...]
    write_mask: tuple[bool, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "token_ids", tuple(int(token) for token in self.token_ids))
        object.__setattr__(
            self,
            "teacher_logprobs",
            tuple(float(value) for value in self.teacher_logprobs),
        )
        object.__setattr__(
            self,
            "student_logprobs",
            tuple(float(value) for value in self.student_logprobs),
        )
        object.__setattr__(self, "write_mask", tuple(bool(value) for value in self.write_mask))
        if self.step < 0:
            raise TokenLogprobError("step must be non-negative")
        length = len(self.token_ids)
        if length == 0:
            raise TokenLogprobError("token sample must not be empty")
        if not (
            len(self.teacher_logprobs)
            == len(self.student_logprobs)
            == len(self.write_mask)
            == length
        ):
            raise TokenLogprobError("token_ids, logprobs, and write_mask must align")
        for value in (*self.teacher_logprobs, *self.student_logprobs):
            if not math.isfinite(value):
                raise TokenLogprobError("log-probabilities must be finite")
        if any(value > 0.0 for value in (*self.teacher_logprobs, *self.student_logprobs)):
            raise TokenLogprobError("log-probabilities must be <= 0")

    @property
    def write_tokens(self) -> int:
        return sum(1 for flagged in self.write_mask if flagged)

    def token_deltas(self) -> tuple[float, ...]:
        """Teacher minus student log-probability for every token."""

        return tuple(
            teacher - student
            for teacher, student in zip(self.teacher_logprobs, self.student_logprobs)
        )


@dataclass(frozen=True)
class MemoryWriteAdvantage:
    """Token-level OPD advantage with extra weight on memory-write tokens."""

    step: int
    per_token: tuple[float, ...]
    write_mean: float
    weighted_mean: float
    write_tokens: int

    def to_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "per_token": list(self.per_token),
            "write_mean": self.write_mean,
            "weighted_mean": self.weighted_mean,
            "write_tokens": self.write_tokens,
        }


@dataclass(frozen=True)
class OpdReport:
    """Aggregate memory-write OPD signal for one batch."""

    samples: int
    write_tokens: int
    mean_delta: float
    mean_write_advantage: float
    weighted_advantage: float
    accepted: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "samples": self.samples,
            "write_tokens": self.write_tokens,
            "mean_delta": self.mean_delta,
            "mean_write_advantage": self.mean_write_advantage,
            "weighted_advantage": self.weighted_advantage,
            "accepted": self.accepted,
        }


@dataclass(frozen=True)
class Camp2OpdDistiller:
    """Asynchronous OPD scoring focused on memory-write tokens."""

    memory_write_weight: float = 2.0
    min_write_tokens: int = 1
    _reports: tuple[OpdReport, ...] = field(default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.memory_write_weight < 1.0:
            raise TokenLogprobError("memory_write_weight must be >= 1.0")
        if self.min_write_tokens < 1:
            raise TokenLogprobError("min_write_tokens must be >= 1")

    def score(self, sample: MemoryWriteSample) -> MemoryWriteAdvantage:
        """Compute per-token advantages, upweighting memory-write tokens."""

        per_token = tuple(
            delta * self.memory_write_weight if write else delta
            for delta, write in zip(sample.token_deltas(), sample.write_mask)
        )
        write_values = [
            delta
            for delta, write in zip(sample.token_deltas(), sample.write_mask)
            if write
        ]
        write_mean = sum(write_values) / len(write_values) if write_values else 0.0
        weighted_mean = sum(per_token) / len(per_token) if per_token else 0.0
        return MemoryWriteAdvantage(
            step=sample.step,
            per_token=per_token,
            write_mean=write_mean,
            weighted_mean=weighted_mean,
            write_tokens=len(write_values),
        )

    def distill(
        self, samples: list[MemoryWriteSample] | tuple[MemoryWriteSample, ...]
    ) -> OpdReport:
        """Aggregate a batch and decide whether it carries a usable signal."""

        if not samples:
            return OpdReport(
                samples=0,
                write_tokens=0,
                mean_delta=0.0,
                mean_write_advantage=0.0,
                weighted_advantage=0.0,
                accepted=False,
            )
        advantages = [self.score(sample) for sample in samples]
        all_deltas = [delta for sample in samples for delta in sample.token_deltas()]
        write_values = [
            delta
            for sample in samples
            for delta, write in zip(sample.token_deltas(), sample.write_mask)
            if write
        ]
        total_tokens = sum(len(sample.token_ids) for sample in samples)
        weighted = sum(value for advantage in advantages for value in advantage.per_token)
        write_tokens = sum(advantage.write_tokens for advantage in advantages)
        return OpdReport(
            samples=len(samples),
            write_tokens=write_tokens,
            mean_delta=sum(all_deltas) / len(all_deltas),
            mean_write_advantage=(
                sum(write_values) / len(write_values) if write_values else 0.0
            ),
            weighted_advantage=weighted / total_tokens if total_tokens else 0.0,
            accepted=write_tokens >= self.min_write_tokens and weighted > 0.0,
        )
