"""Stateful programmatic tool calling and deterministic graph plans.

Ledger item #123 (arXiv:2611.01446 / 2611.20398). The module keeps typed
variables across calls inside one sandbox session, retries transient tool
failures, and executes a fixed graph-query plan without asking a model to
choose each step. It is a deterministic bookkeeper: callers supply the
executor and no network or subprocess is started here.

Feature flag: ``UC_STATEFUL_PTC``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "CallRecord",
    "DeterministicPlan",
    "PlanReport",
    "ProgrammaticSession",
    "STATEFUL_PTC_ENV",
    "ToolCall",
    "TransientToolError",
    "TypedValue",
    "stateful_ptc_enabled",
]

STATEFUL_PTC_ENV = "UC_STATEFUL_PTC"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def stateful_ptc_enabled() -> bool:
    return os.environ.get(STATEFUL_PTC_ENV, "").strip().lower() in _TRUTHY


class TransientToolError(RuntimeError):
    """A retryable tool failure (rate limit, cold start, dropped socket)."""


@dataclass(frozen=True)
class TypedValue:
    name: str
    value: Any
    type_name: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("name must not be empty")
        if not self.type_name.strip():
            raise ValueError("type_name must not be empty")


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    output: str = ""
    result_type: str = "Any"

    def __post_init__(self) -> None:
        if not self.tool.strip():
            raise ValueError("tool must not be empty")
        if not self.output.strip():
            raise ValueError("output must not be empty")
        if not self.result_type.strip():
            raise ValueError("result_type must not be empty")


@dataclass(frozen=True)
class CallRecord:
    tool: str
    output: str
    ok: bool
    attempts: int
    value: Any = None
    error: str = ""


@dataclass(frozen=True)
class PlanReport:
    records: tuple[CallRecord, ...]
    retries: int
    complete: bool

    def output(self, name: str) -> Any:
        for record in self.records:
            if record.output == name and record.ok:
                return record.value
        raise KeyError(name)

    def to_dict(self) -> dict[str, object]:
        return {
            "records": [
                {
                    "tool": r.tool,
                    "output": r.output,
                    "ok": r.ok,
                    "attempts": r.attempts,
                    "error": r.error,
                }
                for r in self.records
            ],
            "retries": self.retries,
            "complete": self.complete,
        }


class ProgrammaticSession:
    """Typed state shared by every call in one sandbox session."""

    def __init__(self, max_variables: int = 128, max_attempts: int = 2) -> None:
        if max_variables < 1:
            raise ValueError("max_variables must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.max_variables = max_variables
        self.max_attempts = max_attempts
        self._values: dict[str, TypedValue] = {}

    def set(self, name: str, value: Any, type_name: str = "Any") -> TypedValue:
        if name not in self._values and len(self._values) >= self.max_variables:
            raise ValueError("variable capacity reached")
        typed = TypedValue(name, value, type_name)
        self._values[name] = typed
        return typed

    def get(self, name: str, expected_type: str | None = None) -> Any:
        typed = self._values.get(name)
        if typed is None:
            raise KeyError(name)
        if expected_type is not None and typed.type_name != expected_type:
            raise TypeError(f"{name} is {typed.type_name}, not {expected_type}")
        return typed.value

    def typed(self, name: str) -> TypedValue:
        try:
            return self._values[name]
        except KeyError:
            raise KeyError(name) from None

    def snapshot(self) -> dict[str, Any]:
        return {name: typed.value for name, typed in self._values.items()}

    def clear(self) -> None:
        self._values.clear()

    def call(
        self,
        call: ToolCall,
        executor: Callable[[str, Mapping[str, Any]], Any],
    ) -> CallRecord:
        """Execute one tool call, retrying only transient failures."""
        attempts = 0
        last_error = ""
        while attempts < self.max_attempts:
            attempts += 1
            try:
                value = executor(call.tool, call.args)
            except TransientToolError as exc:
                last_error = str(exc)
                continue
            except Exception as exc:  # non-transient failures are final
                return CallRecord(call.tool, call.output, False, attempts, None, str(exc))
            self.set(call.output, value, call.result_type)
            return CallRecord(call.tool, call.output, True, attempts, value)
        return CallRecord(call.tool, call.output, False, attempts, None, last_error)


class DeterministicPlan:
    """A fixed sequence of tool calls with no model-chosen branch."""

    def __init__(self, steps: Iterable[ToolCall]) -> None:
        self.steps = tuple(steps)
        if not self.steps:
            raise ValueError("plan must contain at least one step")

    def execute(
        self,
        session: ProgrammaticSession,
        executor: Callable[[str, Mapping[str, Any]], Any],
    ) -> PlanReport:
        records: list[CallRecord] = []
        retries = 0
        for step in self.steps:
            record = session.call(step, executor)
            records.append(record)
            retries += max(0, record.attempts - 1)
            if not record.ok:
                return PlanReport(tuple(records), retries, False)
        return PlanReport(tuple(records), retries, True)
