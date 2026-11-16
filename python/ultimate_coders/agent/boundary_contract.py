"""File-role boundaries for the worker side of BoundaryContract (#7).

``crates/uc-types/src/contract.rs`` is the authoritative type. The Rust engine
does not yet expose it to Python, so this module mirrors the small, stable part
the worker can actually act on:

* unlisted paths are ``inspect_only`` (fail closed);
* only ``editable`` paths may be written;
* ``required`` and ``protected`` paths are also write-denied;
* every other role can only be narrowed, never widened.

The contract is built from an :class:`~ultimate_coders.agent.planner.ImplementationSpec`:
target files become ``editable`` and the dependency closure becomes
``inspect_only``. A caller may add explicit ``file_constraints`` as editable.
Challenges and engine hot-updates remain the Rust-side integration boundary.

Feature flag ``UC_BOUNDARY_CONTRACT`` gates the worker caller. A second flag
``UC_BOUNDARY_CONTRACT_ENFORCE`` turns a violation into an attempt failure;
without it the gate is diagnostic.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

__all__ = [
    "BOUNDARY_CONTRACT_ENV",
    "BOUNDARY_ENFORCE_ENV",
    "BoundaryContract",
    "BoundaryContractError",
    "BoundaryRole",
    "BoundaryViolation",
    "BoundaryEntry",
    "boundary_contract_enabled",
    "boundary_contract_enforce_enabled",
    "normalize_path",
]

BOUNDARY_CONTRACT_ENV = "UC_BOUNDARY_CONTRACT"
BOUNDARY_ENFORCE_ENV = "UC_BOUNDARY_CONTRACT_ENFORCE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str) -> bool:
    raw = os.environ.get(name)
    return raw is not None and raw.strip().lower() in _TRUTHY


def boundary_contract_enabled() -> bool:
    """Feature flag: build and check a boundary contract (default off)."""

    return _env_flag(BOUNDARY_CONTRACT_ENV)


def boundary_contract_enforce_enabled() -> bool:
    """Whether a boundary violation fails the attempt instead of only emitting."""

    return _env_flag(BOUNDARY_ENFORCE_ENV)


class BoundaryRole(str, Enum):
    """The role a path plays inside a task's boundary."""

    EDITABLE = "editable"
    INSPECT_ONLY = "inspect_only"
    REQUIRED = "required"
    PROTECTED = "protected"

    @property
    def strictness(self) -> int:
        return {
            BoundaryRole.EDITABLE: 0,
            BoundaryRole.INSPECT_ONLY: 1,
            BoundaryRole.REQUIRED: 2,
            BoundaryRole.PROTECTED: 3,
        }[self]

    @property
    def editable(self) -> bool:
        return self is BoundaryRole.EDITABLE


def normalize_path(value: Any) -> str:
    """Normalize a workspace-relative path to a stable posix spelling."""

    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return PurePosixPath(text).as_posix() if text else ""


class BoundaryContractError(ValueError):
    """A boundary contract is malformed or a narrowing attempt widened it."""


def _role(value: Any) -> BoundaryRole:
    if isinstance(value, BoundaryRole):
        return value
    text = str(value or "").strip().lower().replace("-", "_")
    try:
        return BoundaryRole(text)
    except ValueError:
        raise BoundaryContractError(f"unknown boundary role {value!r}") from None


@dataclass(frozen=True)
class BoundaryEntry:
    """One explicit path role."""

    path: str
    role: BoundaryRole
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "role": self.role.value, "reason": self.reason}


@dataclass(frozen=True)
class BoundaryViolation:
    """A changed path the contract does not permit the task to write."""

    path: str
    role: BoundaryRole
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "role": self.role.value, "reason": self.reason}


@dataclass(frozen=True)
class BoundaryContract:
    """A deterministic, immutable map of workspace paths to roles."""

    entries: Mapping[str, BoundaryRole] = field(default_factory=dict)
    version: int = 1

    def __post_init__(self) -> None:
        normalized: dict[str, BoundaryRole] = {}
        for raw_path, raw_role in dict(self.entries).items():
            path = normalize_path(raw_path)
            if not path:
                raise BoundaryContractError("boundary contract contains an empty path")
            role = _role(raw_role)
            previous = normalized.get(path)
            if previous is not None and previous is not role:
                raise BoundaryContractError(f"duplicate boundary path {path!r}")
            normalized[path] = role
        object.__setattr__(self, "entries", normalized)
        try:
            version = int(self.version)
        except (TypeError, ValueError):
            raise BoundaryContractError(
                f"version must be an integer, got {self.version!r}"
            ) from None
        if version < 1:
            raise BoundaryContractError("version must be >= 1")
        object.__setattr__(self, "version", version)

    @classmethod
    def from_entries(
        cls,
        entries: Iterable[BoundaryEntry],
        *,
        version: int = 1,
    ) -> BoundaryContract:
        return cls(entries={entry.path: entry.role for entry in entries}, version=version)

    @classmethod
    def from_implementation_spec(
        cls,
        spec: Any,
        *,
        file_constraints: Sequence[str] = (),
        version: int = 1,
    ) -> BoundaryContract:
        """Build the worker-side contract from a PerceptionPass spec.

        ``target_files`` are the files the implementation may edit; the
        dependency closure is read-only. Explicit ``file_constraints`` are
        treated as editable too, matching the existing prompt-level contract.
        """

        editable: list[str] = []
        for boundary in getattr(spec, "target_files", ()) or ():
            raw_path = getattr(boundary, "path", None) or (
                boundary.get("path") if isinstance(boundary, Mapping) else None
            )
            path = normalize_path(raw_path)
            if path and path not in editable:
                editable.append(path)
        for raw_path in file_constraints:
            path = normalize_path(raw_path)
            if path and path not in editable:
                editable.append(path)

        entries: dict[str, BoundaryRole] = {path: BoundaryRole.EDITABLE for path in editable}
        for raw_path in getattr(spec, "dependency_closure", ()) or ():
            path = normalize_path(raw_path)
            if path and path not in entries:
                entries[path] = BoundaryRole.INSPECT_ONLY
        return cls(entries=entries, version=version)

    def role_for(self, path: str) -> BoundaryRole:
        """Return the role for ``path``; unlisted paths default to inspect-only."""

        return self.entries.get(normalize_path(path), BoundaryRole.INSPECT_ONLY)

    def editable_paths(self) -> tuple[str, ...]:
        return tuple(sorted(path for path, role in self.entries.items() if role.editable))

    def violations(self, changed_paths: Iterable[str]) -> list[BoundaryViolation]:
        """Return one violation for every changed path that is not editable."""

        found: list[BoundaryViolation] = []
        seen: set[str] = set()
        for raw_path in changed_paths:
            path = normalize_path(raw_path)
            if not path or path in seen:
                continue
            seen.add(path)
            role = self.role_for(path)
            if not role.editable:
                found.append(BoundaryViolation(path=path, role=role))
        return found

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "entries": {
                path: role.value for path, role in sorted(self.entries.items())
            },
        }
