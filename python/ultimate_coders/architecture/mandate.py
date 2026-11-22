"""Immutable Mandate envelopes and monotonic (narrowing-only) delegation.

A mandate is the capability/resource envelope an attempt is allowed to act
inside: which capabilities it may invoke, on which resources, how many attempts
and tokens it may consume, and when it stops being valid.  Delegating to a
child (a subtask, a retry, a nested agent) must never widen the envelope, so
:func:`narrow` is the only supported way to derive one mandate from another:

* capabilities must be a subset of the parent's;
* resources must be a subset of the parent's (``resources`` is an allow-list;
  an empty tuple grants no resources);
* ``max_attempts`` / ``budget_tokens`` may only shrink;
* the expiry may not move past a parent that already expires (a child with no
  explicit expiry inherits the parent's instead of becoming immortal);
* the subject (the task identity the mandate was issued for) may not change.

The result is a new frozen :class:`Mandate` with ``parent_id`` pointing at the
parent; the parent object is never mutated.  :meth:`Mandate.digest` is a stable
SHA-256 over canonical JSON so the same envelope always hashes identically
across processes and storage formats.

The feature flag ``UC_MANDATE_ENVELOPE`` gates the caller, not the dataclass:
the module is safe to import and use while the flag is unset.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

__all__ = [
    "Mandate",
    "MandateError",
    "canonical_json",
    "digest",
    "mandate_envelope_enabled",
    "narrow",
]

_TRUTHY = frozenset({"1", "true", "yes", "on"})
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BUDGET_TOKENS = 20000


class MandateError(ValueError):
    """A mandate envelope is malformed or a narrowing attempt widened it."""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def mandate_envelope_enabled() -> bool:
    """Feature flag: immutable mandate envelopes (default off)."""

    return _env_flag("UC_MANDATE_ENVELOPE")


def canonical_json(payload: Any) -> str:
    """Deterministic JSON used for mandate digests."""

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def digest(value: Any) -> str:
    """Stable SHA-256 hex digest of any JSON-serialisable value."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _dedupe(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, Iterable):
        values = (values,)
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _metadata(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return MappingProxyType(dict(value))
    return MappingProxyType({})


def _canonical_float(value: Any) -> float:
    if isinstance(value, bool):
        raise MandateError(f"expires_at must be a number, got {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise MandateError(f"expires_at must be a number, got {value!r}") from None
    if not math.isfinite(parsed):
        raise MandateError(f"expires_at must be a finite number, got {value!r}")
    return parsed


@dataclass(frozen=True)
class Mandate:
    """Immutable capability/resource envelope for one subject."""

    mandate_id: str
    subject: str
    capabilities: tuple[str, ...]
    resources: tuple[str, ...] = ()
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    expires_at: float = 0.0
    parent_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "mandate_id", str(self.mandate_id or "").strip())
        object.__setattr__(self, "subject", str(self.subject or "").strip())
        object.__setattr__(self, "capabilities", _dedupe(self.capabilities))
        object.__setattr__(self, "resources", _dedupe(self.resources))
        if isinstance(self.max_attempts, bool):
            raise MandateError(f"max_attempts must be an integer, got {self.max_attempts!r}")
        try:
            max_attempts = int(self.max_attempts)
        except (TypeError, ValueError):
            raise MandateError(
                f"max_attempts must be an integer, got {self.max_attempts!r}"
            ) from None
        if max_attempts <= 0:
            raise MandateError(f"max_attempts must be positive, got {max_attempts}")
        if isinstance(self.budget_tokens, bool):
            raise MandateError(f"budget_tokens must be an integer, got {self.budget_tokens!r}")
        try:
            budget_tokens = int(self.budget_tokens)
        except (TypeError, ValueError):
            raise MandateError(
                f"budget_tokens must be an integer, got {self.budget_tokens!r}"
            ) from None
        if budget_tokens <= 0:
            raise MandateError(f"budget_tokens must be positive, got {budget_tokens}")
        expires_at = _canonical_float(self.expires_at)
        if expires_at < 0.0:
            raise MandateError("expires_at must be zero or a positive timestamp")
        object.__setattr__(self, "max_attempts", max_attempts)
        object.__setattr__(self, "budget_tokens", budget_tokens)
        object.__setattr__(self, "expires_at", expires_at)
        parent_id = self.parent_id
        normalized_parent = None if parent_id is None else str(parent_id).strip()
        object.__setattr__(self, "parent_id", normalized_parent or None)
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mandate_id": self.mandate_id,
            "subject": self.subject,
            "capabilities": list(self.capabilities),
            "resources": list(self.resources),
            "max_attempts": int(self.max_attempts),
            "budget_tokens": int(self.budget_tokens),
            "expires_at": float(self.expires_at),
            "parent_id": self.parent_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> Mandate:
        if not isinstance(row, Mapping):
            raise MandateError("mandate row must be a mapping")
        return cls(
            mandate_id=row.get("mandate_id", ""),
            subject=row.get("subject", ""),
            capabilities=_dedupe(row.get("capabilities")),
            resources=_dedupe(row.get("resources")),
            max_attempts=row.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
            budget_tokens=row.get("budget_tokens", DEFAULT_BUDGET_TOKENS),
            expires_at=row.get("expires_at", 0.0),
            parent_id=row.get("parent_id"),
            metadata=row.get("metadata"),
        )

    def digest(self) -> str:
        """Stable SHA-256 over canonical JSON of the whole envelope."""

        return digest(self.to_dict())

    def is_expired(self, now: float) -> bool:
        """``True`` once ``now`` reaches a positive ``expires_at``."""

        if self.expires_at <= 0.0:
            return False
        current = float(now)
        if not math.isfinite(current):
            return True
        return current >= self.expires_at

    def allows(self, capability: str, resource: str | None = None) -> bool:
        """Whether the envelope grants ``capability`` (on ``resource`` when given).

        ``resources`` is an allow-list: a non-``None`` resource must be listed.
        """

        name = str(capability or "").strip()
        if not name or name not in self.capabilities:
            return False
        if resource is None:
            return True
        return str(resource).strip() in self.resources


def narrow(
    parent: Mandate,
    *,
    mandate_id: str,
    subject: str | None = None,
    capabilities: Sequence[str] | None = None,
    resources: Sequence[str] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    expires_at: float = 0.0,
    metadata: Mapping[str, Any] | None = None,
) -> Mandate:
    """Derive a strictly narrower mandate from ``parent``.

    ``capabilities``/``resources``/``subject``/``metadata`` inherit from the
    parent when omitted.  ``max_attempts`` and ``budget_tokens`` use the
    documented defaults, so a caller narrowing a smaller parent must pass the
    parent's values explicitly.  A child without an explicit expiry inherits a
    finite parent expiry (it can never outlive its parent).
    """

    if not isinstance(parent, Mandate):
        raise MandateError("parent must be a Mandate")

    child_id = str(mandate_id or "").strip()
    if not child_id:
        raise MandateError("mandate_id must be non-empty")

    child_subject = parent.subject if subject is None else str(subject).strip()
    if child_subject != parent.subject:
        raise MandateError(
            f"subject {child_subject!r} differs from parent subject {parent.subject!r}"
        )

    child_capabilities = (
        tuple(parent.capabilities)
        if capabilities is None
        else _dedupe(capabilities)
    )
    granted_capabilities = set(parent.capabilities)
    denied_capabilities = [
        capability
        for capability in child_capabilities
        if capability not in granted_capabilities
    ]
    if denied_capabilities:
        raise MandateError(
            f"capabilities {denied_capabilities!r} are not granted by parent "
            f"{parent.mandate_id!r}"
        )

    child_resources = (
        tuple(parent.resources) if resources is None else _dedupe(resources)
    )
    denied_resources = sorted(set(child_resources) - set(parent.resources))
    if denied_resources:
        raise MandateError(
            f"resources {denied_resources!r} are not granted by parent "
            f"{parent.mandate_id!r}"
        )

    attempts = int(max_attempts)
    if attempts <= 0:
        raise MandateError("max_attempts must be positive")
    if attempts > parent.max_attempts:
        raise MandateError(
            f"max_attempts {attempts} exceeds parent maximum {parent.max_attempts}"
        )

    budget = int(budget_tokens)
    if budget <= 0:
        raise MandateError("budget_tokens must be positive")
    if budget > parent.budget_tokens:
        raise MandateError(
            f"budget_tokens {budget} exceeds parent budget {parent.budget_tokens}"
        )

    child_expires = _canonical_float(expires_at)
    if child_expires < 0.0:
        raise MandateError("expires_at must be zero or a positive timestamp")
    if parent.expires_at > 0.0:
        if child_expires > parent.expires_at:
            raise MandateError(
                f"expires_at {child_expires} exceeds parent expiry {parent.expires_at}"
            )
        if child_expires <= 0.0:
            child_expires = parent.expires_at

    child_metadata = (
        dict(parent.metadata) if metadata is None else _metadata(metadata)
    )
    return Mandate(
        mandate_id=child_id,
        subject=child_subject,
        capabilities=child_capabilities,
        resources=child_resources,
        max_attempts=attempts,
        budget_tokens=budget,
        expires_at=child_expires,
        parent_id=parent.mandate_id,
        metadata=child_metadata,
    )
