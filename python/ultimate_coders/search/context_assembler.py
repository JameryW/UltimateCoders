"""Fixed-prefix context assembly for prompt-cache-friendly agent loops.

An agent loop re-sends a large context on every model call. Serving stacks can
reuse the prefill of an unchanged *prefix*, but only when the prefix is
byte-stable: a single character early in the prompt invalidates every cached
token after it. This module makes the stable prefix an explicit, ordered
contract.

Slots are declared in invalidation-rate order -- the order in which content
changes across a task, from almost never to every turn:

``SYSTEM_SCHEMA``
    The system prompt and tool schema. Constant for the life of a session.
``DECLARATIVE``
    Repository facts: build commands, conventions, capability declarations.
``PROCEDURAL``
    The task procedure or plan. Stable while a subtask runs.
``CHECKPOINT``
    Compacted state from earlier turns; changes on compaction, not per call.
``OBSERVATION``
    Tool output and intermediate results. Changes every turn.

The assembler places slots in exactly this order, refuses a stable slot after
an unstable one, hashes every block with SHA-256, and only ever truncates the
**observation tail**. Stable blocks are never rewritten to satisfy a budget:
doing so would make the cached prefix depend on volatile content. If stable
blocks alone exceed the budget the result reports ``over_budget`` instead of
silently breaking the cache contract.

No generation calls, tokenizer imports, or network access: token estimates use
four characters per token exactly like the context compiler's heuristic.

Feature flag: ``UC_PREFIX_CONTEXT_ASSEMBLY``.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CHECKPOINT",
    "DECLARATIVE",
    "OBSERVATION",
    "PROCEDURAL",
    "SLOT_ORDER",
    "SYSTEM_SCHEMA",
    "AssembledContext",
    "ContextAssembler",
    "ContextBlock",
    "ContextSlot",
    "assemble_context",
    "context_assembler_enabled",
    "estimate_tokens",
]

#: Canonical slot names, in invalidation-rate order (most stable first).
SYSTEM_SCHEMA = "system_schema"
DECLARATIVE = "declarative"
PROCEDURAL = "procedural"
CHECKPOINT = "checkpoint"
OBSERVATION = "observation"

#: The fixed slot order. Every assembled context follows it.
SLOT_ORDER = (SYSTEM_SCHEMA, DECLARATIVE, PROCEDURAL, CHECKPOINT, OBSERVATION)

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_CHARS_PER_TOKEN = 4
_SLOT_INDEX = {slot: index for index, slot in enumerate(SLOT_ORDER)}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _truthy(value: str | None) -> bool:
    return _text(value).strip().lower() in _TRUTHY_VALUES


def estimate_tokens(text: str) -> int:
    """Deterministic, dependency-free token estimate (4 characters/token)."""
    return (len(_text(text)) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _sha256(text: str) -> str:
    return hashlib.sha256(_text(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContextSlot:
    """One caller-supplied slot value.

    ``stable`` marks content that is byte-identical across turns. Observations
    should be marked ``stable=False`` so they form the invalidatable suffix.
    """

    slot: str
    content: str = ""
    stable: bool = True

    def __post_init__(self) -> None:
        slot = _text(self.slot).strip().lower()
        if slot not in _SLOT_INDEX:
            raise ValueError(f"unknown context slot: {self.slot!r}")
        object.__setattr__(self, "slot", slot)
        object.__setattr__(self, "content", _text(self.content))
        object.__setattr__(self, "stable", bool(self.stable))


class ContextBlock(dict):
    """A rendered block, readable both as a mapping and as attributes.

    Keys: ``slot``, ``content``, ``stable``, ``sha256``, ``tokens`` and
    ``truncated``. The dict form keeps the assembled context trivially
    serializable without a second conversion step.
    """

    def __init__(
        self,
        slot: str,
        content: str,
        *,
        stable: bool,
        sha256: str,
        tokens: int,
        truncated: bool = False,
    ) -> None:
        super().__init__(
            slot=slot,
            content=content,
            stable=stable,
            sha256=sha256,
            tokens=tokens,
            truncated=truncated,
        )

    @property
    def slot(self) -> str:
        return self["slot"]

    @property
    def content(self) -> str:
        return self["content"]

    @property
    def stable(self) -> bool:
        return self["stable"]

    @property
    def sha256(self) -> str:
        return self["sha256"]

    @property
    def tokens(self) -> int:
        return self["tokens"]

    @property
    def truncated(self) -> bool:
        return self["truncated"]


@dataclass
class AssembledContext:
    """The assembled fixed-prefix context.

    ``block_sha256`` holds one digest per block, in block order. ``full_sha256``
    is the digest of the concatenated block digests, so it changes whenever any
    block changes. ``prefix_cache_hint`` is an opaque digest of the stable
    blocks only: two calls with the same hint can reuse the same cached prefix.
    """

    blocks: list[ContextBlock] = field(default_factory=list)
    block_sha256: list[str] = field(default_factory=list)
    total_tokens: int = 0
    prefix_cache_hint: str = ""
    budget_tokens: int = 0
    truncated: bool = False
    over_budget: bool = False

    @property
    def full_sha256(self) -> str:
        """Digest over every block digest, in order."""
        return _sha256("".join(self.block_sha256))

    @property
    def context_sha256(self) -> str:
        """Alias for :attr:`full_sha256` for callers that name it by content."""
        return self.full_sha256

    @property
    def stable_prefix(self) -> list[ContextBlock]:
        """The leading stable blocks that a cache-aware stack may reuse."""
        return [block for block in self.blocks if block.stable]

    @property
    def stable_tokens(self) -> int:
        return sum(block.tokens for block in self.blocks if block.stable)

    def changed_suffix(self, previous: AssembledContext | None) -> list[ContextBlock]:
        """Return only the blocks that differ from ``previous``'s suffix.

        The comparison walks block digests in order and returns every block from
        the first mismatch to the end. With ``previous=None`` all blocks are
        new. The leading unchanged blocks are the reusable prefix.
        """
        if previous is None:
            return list(self.blocks)
        prefix = 0
        for old_hash, new_hash in zip(previous.block_sha256, self.block_sha256):
            if old_hash != new_hash:
                break
            prefix += 1
        return list(self.blocks[prefix:])

    def render(self) -> str:
        """Join the block contents with blank lines, in slot order."""
        return "\n\n".join(block.content for block in self.blocks if block.content)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": [dict(block) for block in self.blocks],
            "block_sha256": list(self.block_sha256),
            "full_sha256": self.full_sha256,
            "total_tokens": self.total_tokens,
            "prefix_cache_hint": self.prefix_cache_hint,
            "budget_tokens": self.budget_tokens,
            "truncated": self.truncated,
            "over_budget": self.over_budget,
        }


def _coerce_slots(slots: Iterable[ContextSlot] | Mapping[str, Any]) -> list[ContextSlot]:
    if isinstance(slots, Mapping):
        return [ContextSlot(slot, content) for slot, content in slots.items()]
    coerced: list[ContextSlot] = []
    for item in slots or ():
        if isinstance(item, ContextSlot):
            coerced.append(item)
        elif isinstance(item, Mapping):
            coerced.append(ContextSlot(**dict(item)))
        else:
            raise TypeError(f"expected ContextSlot, got {type(item).__name__}")
    return coerced


def _make_block(slot: ContextSlot, content: str, *, truncated: bool = False) -> ContextBlock:
    return ContextBlock(
        slot=slot.slot,
        content=content,
        stable=slot.stable,
        sha256=_sha256(content),
        tokens=estimate_tokens(content),
        truncated=truncated,
    )


class ContextAssembler:
    """Assemble caller slots into a deterministic fixed-prefix context."""

    def assemble(
        self,
        slots: Sequence[ContextSlot] | Mapping[str, Any],
        *,
        budget_tokens: int = 8192,
    ) -> AssembledContext:
        """Order, hash and (observation-tail) truncate the provided slots.

        Raises ``ValueError`` for duplicate or unknown slots and for a stable
        slot that appears after an unstable one. Missing slots are allowed so a
        caller can assemble a partial context; the order of those present is
        still canonical.
        """
        budget = int(budget_tokens)
        if budget < 0:
            raise ValueError("budget_tokens must be >= 0")
        ordered = _coerce_slots(slots)
        seen_slots: set[str] = set()
        for slot in ordered:
            if slot.slot in seen_slots:
                raise ValueError(f"duplicate context slot: {slot.slot!r}")
            seen_slots.add(slot.slot)
        ordered = sorted(ordered, key=lambda slot: _SLOT_INDEX[slot.slot])

        seen_unstable = False
        for slot in ordered:
            if slot.stable and seen_unstable:
                raise ValueError("stable slots must precede unstable slots")
            if not slot.stable:
                seen_unstable = True

        blocks = [_make_block(slot, slot.content) for slot in ordered]
        truncated = False
        observation_index = next(
            (index for index, block in enumerate(blocks) if block.slot == OBSERVATION),
            None,
        )
        if observation_index is not None:
            other_tokens = sum(
                block.tokens for block in blocks if block.slot != OBSERVATION
            )
            observation = blocks[observation_index]
            remaining_tokens = max(0, budget - other_tokens)
            if observation.tokens > remaining_tokens:
                max_chars = remaining_tokens * _CHARS_PER_TOKEN
                content = observation.content[:max_chars]
                blocks[observation_index] = _make_block(
                    ordered[observation_index], content, truncated=True
                )
                truncated = True

        total_tokens = sum(block.tokens for block in blocks)
        stable_hashes = [block.sha256 for block in blocks if block.stable]
        hint = _sha256("|".join(stable_hashes)) if stable_hashes else ""
        return AssembledContext(
            blocks=blocks,
            block_sha256=[block.sha256 for block in blocks],
            total_tokens=total_tokens,
            prefix_cache_hint=hint,
            budget_tokens=budget,
            truncated=truncated,
            over_budget=total_tokens > budget,
        )


def assemble_context(
    slots: Sequence[ContextSlot] | Mapping[str, Any],
    *,
    budget_tokens: int = 8192,
) -> AssembledContext:
    """Module-level convenience wrapper around :meth:`ContextAssembler.assemble`."""
    return ContextAssembler().assemble(slots, budget_tokens=budget_tokens)


def context_assembler_enabled() -> bool:
    """Whether ``UC_PREFIX_CONTEXT_ASSEMBLY`` enables fixed-prefix assembly."""
    return _truthy(os.environ.get("UC_PREFIX_CONTEXT_ASSEMBLY"))
