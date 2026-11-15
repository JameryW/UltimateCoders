"""Dressage-compatible token-in/token-out (TITO) trajectory export.

One JSONL line records one attempt as ordered turns of token ids, per-token
log-probabilities and a 0/1 loss mask, which is what a TITO trainer replays
without re-tokenizing.  Export is opt-in via ``UC_TRAJECTORY_EXPORT`` plus
``UC_TRAJECTORY_EXPORT_DIR``; disabled exporters are inert no-ops.

Files are append-only with a byte budget: ``trajectories.jsonl`` is the active
file and rotates to ``trajectories-<n>.jsonl`` when a write would exceed
``max_file_bytes``.  Obvious credentials are redacted before serialization.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_EXPORT_ENABLED_ENV = "UC_TRAJECTORY_EXPORT"
_EXPORT_DIR_ENV = "UC_TRAJECTORY_EXPORT_DIR"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_ACTIVE_FILENAME = "trajectories.jsonl"
_ROTATED_TEMPLATE = "trajectories-{index}.jsonl"
_OUTCOMES = ("failed", "rescued", "succeeded")
_PAIRABLE_OUTCOMES = ("failed", "rescued")
_REDACTED = "[redacted]"
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"Bearer\s+\S+"),
)
_SENSITIVE_METADATA_KEY_PARTS = ("api_key", "token", "secret")


@dataclass(frozen=True)
class TITOToken:
    """A single token with its log-probability and training loss mask."""

    token_id: int
    logprob: float
    loss_mask: int

    def __post_init__(self) -> None:
        if self.loss_mask not in (0, 1):
            raise ValueError("loss_mask must be 0 or 1")
        object.__setattr__(self, "token_id", int(self.token_id))
        object.__setattr__(self, "logprob", float(self.logprob))

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "logprob": self.logprob,
            "loss_mask": self.loss_mask,
        }


@dataclass(frozen=True)
class TrajectoryTurn:
    """One chat turn aligned token-by-token."""

    role: str
    token_ids: tuple[int, ...]
    logprobs: tuple[float, ...]
    loss_mask: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("role must be a non-empty string")
        token_ids = tuple(int(item) for item in self.token_ids)
        logprobs = tuple(float(item) for item in self.logprobs)
        loss_mask = tuple(int(item) for item in self.loss_mask)
        if not len(token_ids) == len(logprobs) == len(loss_mask):
            raise ValueError(
                "token_ids, logprobs and loss_mask must have the same length"
            )
        if any(mask not in (0, 1) for mask in loss_mask):
            raise ValueError("loss_mask entries must be 0 or 1")
        object.__setattr__(self, "token_ids", token_ids)
        object.__setattr__(self, "logprobs", logprobs)
        object.__setattr__(self, "loss_mask", loss_mask)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "token_ids": list(self.token_ids),
            "logprobs": list(self.logprobs),
            "loss_mask": list(self.loss_mask),
        }


@dataclass(frozen=True)
class TrajectoryRecord:
    """A complete attempt trajectory, exportable as one JSONL line."""

    task_id: str
    subtask_id: str
    attempt: int
    outcome: str
    turns: tuple[TrajectoryTurn, ...]
    created_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not isinstance(self.subtask_id, str):
            raise TypeError("task_id and subtask_id must be strings")
        if int(self.attempt) < 0:
            raise ValueError("attempt must be non-negative")
        if self.outcome not in _OUTCOMES:
            raise ValueError(f"outcome must be one of {_OUTCOMES}")
        turns = tuple(self.turns)
        if not all(isinstance(turn, TrajectoryTurn) for turn in turns):
            raise TypeError("turns must contain TrajectoryTurn values")
        if not isinstance(self.created_at, str):
            raise TypeError("created_at must be a string")
        object.__setattr__(self, "attempt", int(self.attempt))
        object.__setattr__(self, "turns", turns)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "attempt": self.attempt,
            "outcome": self.outcome,
            "turns": [turn.to_dict() for turn in self.turns],
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


class TrajectoryExporter:
    """Append TITO trajectory records to a rotating JSONL file."""

    def __init__(
        self,
        directory: str | Path,
        enabled: bool | None = None,
        max_file_bytes: int = 8_000_000,
        now: Callable[[], datetime] | None = None,
        *,
        fsync: bool = False,
    ) -> None:
        if int(max_file_bytes) <= 0:
            raise ValueError("max_file_bytes must be positive")
        self.directory = Path(directory)
        self.enabled = trajectory_export_enabled() if enabled is None else bool(enabled)
        self.max_file_bytes = int(max_file_bytes)
        self._now = now
        self.fsync = bool(fsync)

    @property
    def active_path(self) -> Path:
        return self.directory / _ACTIVE_FILENAME

    def write(self, record: TrajectoryRecord) -> Path | None:
        """Append ``record`` and return the file written, or ``None`` when disabled."""

        if not self.enabled:
            return None
        if not record.created_at:
            record = replace(record, created_at=self._timestamp())
        line = json.dumps(
            _sanitize_record(record),
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.active_path
        if path.exists():
            size = path.stat().st_size
            if size > 0 and size + len(line.encode("utf-8")) > self.max_file_bytes:
                path = self._rotate()
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        return path

    def _rotate(self) -> Path:
        active = self.active_path
        index = 1
        while (self.directory / _ROTATED_TEMPLATE.format(index=index)).exists():
            index += 1
        active.replace(self.directory / _ROTATED_TEMPLATE.format(index=index))
        return active

    def _timestamp(self) -> str:
        moment = self._now() if self._now is not None else datetime.now(timezone.utc)
        return moment.isoformat()


def pair_outcomes(
    records: Iterable[TrajectoryRecord],
) -> list[tuple[TrajectoryRecord, TrajectoryRecord]]:
    """Pair ``failed`` with ``rescued`` records on ``(task_id, subtask_id)``.

    Pairs are ordered by group key and then by ``(attempt, created_at)``, so
    the same input always produces the same output.  Extra records on either
    side of a group are ignored, as are ``succeeded`` records.
    """

    grouped: dict[tuple[str, str], dict[str, list[TrajectoryRecord]]] = {}
    for record in records:
        if record.outcome not in _PAIRABLE_OUTCOMES:
            continue
        key = (record.task_id, record.subtask_id)
        bucket = grouped.setdefault(key, {"failed": [], "rescued": []})
        bucket[record.outcome].append(record)
    pairs: list[tuple[TrajectoryRecord, TrajectoryRecord]] = []
    for key in sorted(grouped):
        failed = sorted(grouped[key]["failed"], key=_record_order)
        rescued = sorted(grouped[key]["rescued"], key=_record_order)
        pairs.extend(zip(failed, rescued))
    return pairs


def trajectory_export_enabled() -> bool:
    """Return whether export is enabled and has a destination directory."""

    if os.environ.get(_EXPORT_ENABLED_ENV, "").strip().lower() not in _TRUTHY:
        return False
    return bool(os.environ.get(_EXPORT_DIR_ENV, "").strip())


def _record_order(record: TrajectoryRecord) -> tuple[int, str]:
    return (record.attempt, record.created_at)


def _sanitize_record(record: TrajectoryRecord) -> dict[str, Any]:
    payload = record.to_dict()
    payload["metadata"] = _redact_metadata(record.metadata)
    for key, value in list(payload.items()):
        if key != "metadata":
            payload[key] = _redact_strings(value)
    return payload


def _redact_metadata(value: Any) -> Any:
    """Recursively redact metadata: sensitive keys are replaced wholesale."""

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_metadata_key(key_text):
                redacted[key_text] = _REDACTED
            else:
                redacted[key_text] = _redact_metadata(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_metadata(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _redact_strings(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Mapping):
        return {str(key): _redact_strings(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_strings(item) for item in value]
    return value


def _redact_string(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def _is_sensitive_metadata_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_METADATA_KEY_PARTS)
