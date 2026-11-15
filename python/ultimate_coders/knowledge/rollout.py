"""Gradual rollout gates for the evidence-first knowledge layer.

The V2.0 plan ships the layer behind disabled-by-default flags and widens it
one stage at a time (Stage A Disabled -> B Shadow -> C Recovery -> D Graph ->
E Canonical). This module is the single place that reads those flags so the
worker, the ledgers and the dashboard agree on what is enabled.

All flags are environment variables (``UC_*`` convention) and every unknown or
malformed value fails closed to :data:`KnowledgeStage.DISABLED`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import IntEnum
from typing import Any

__all__ = [
    "ATTEMPT_CHAIN_ROUNDS_ENV",
    "DEFAULT_ATTEMPT_CHAIN_MAX_ROUNDS",
    "DEFAULT_RECOVERY_BUDGET_TOKENS",
    "DEFAULT_RETRIEVAL_BUDGET_TOKENS",
    "KnowledgeStage",
    "MAX_PATCH_FILES_ENV",
    "MAX_PATCH_DIFF_CHARS_ENV",
    "RECOVERY_BUDGET_ENV",
    "RETRIEVAL_BUDGET_ENV",
    "STAGE_ENV",
    "canonical_enabled",
    "capture_enabled",
    "chain_max_rounds",
    "describe_stage",
    "graph_sharing_enabled",
    "max_patch_diff_chars",
    "max_patch_files",
    "recovery_budget_tokens",
    "recovery_enabled",
    "resolve_stage",
    "retrieval_budget_tokens",
    "stage_from_env",
]

STAGE_ENV = "UC_KNOWLEDGE_STAGE"
RECOVERY_BUDGET_ENV = "UC_RECOVERY_BUDGET_TOKENS"
RETRIEVAL_BUDGET_ENV = "UC_KNOWLEDGE_RETRIEVAL_BUDGET_TOKENS"
ATTEMPT_CHAIN_ROUNDS_ENV = "UC_ATTEMPT_CHAIN_MAX_ROUNDS"
MAX_PATCH_FILES_ENV = "UC_KNOWLEDGE_MAX_PATCH_FILES"
MAX_PATCH_DIFF_CHARS_ENV = "UC_KNOWLEDGE_MAX_DIFF_CHARS"

DEFAULT_RECOVERY_BUDGET_TOKENS = 600
DEFAULT_RETRIEVAL_BUDGET_TOKENS = 500
DEFAULT_ATTEMPT_CHAIN_MAX_ROUNDS = 20
DEFAULT_MAX_PATCH_FILES = 20
DEFAULT_MAX_PATCH_DIFF_CHARS = 20_000


class KnowledgeStage(IntEnum):
    """Rollout stage of the knowledge layer (ordered, fail-closed)."""

    DISABLED = 0
    SHADOW = 1
    RECOVERY = 2
    GRAPH = 3
    CANONICAL = 4


_STAGE_ALIASES = {
    "": KnowledgeStage.DISABLED,
    "0": KnowledgeStage.DISABLED,
    "off": KnowledgeStage.DISABLED,
    "disabled": KnowledgeStage.DISABLED,
    "none": KnowledgeStage.DISABLED,
    "1": KnowledgeStage.SHADOW,
    "shadow": KnowledgeStage.SHADOW,
    "2": KnowledgeStage.RECOVERY,
    "recovery": KnowledgeStage.RECOVERY,
    "canary": KnowledgeStage.RECOVERY,
    "3": KnowledgeStage.GRAPH,
    "graph": KnowledgeStage.GRAPH,
    "graph-wide": KnowledgeStage.GRAPH,
    "graph_wide": KnowledgeStage.GRAPH,
    "4": KnowledgeStage.CANONICAL,
    "canonical": KnowledgeStage.CANONICAL,
    "repository": KnowledgeStage.CANONICAL,
    "repo": KnowledgeStage.CANONICAL,
    "on": KnowledgeStage.CANONICAL,
    "all": KnowledgeStage.CANONICAL,
}


def resolve_stage(value: Any) -> KnowledgeStage:
    """Coerce a raw flag value into a :class:`KnowledgeStage`.

    Unknown values fail closed to DISABLED: a typo in an env var must never
    silently enable knowledge injection.
    """

    if isinstance(value, KnowledgeStage):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return KnowledgeStage(value)
        except ValueError:
            return KnowledgeStage.DISABLED
    key = str(value or "").strip().lower()
    return _STAGE_ALIASES.get(key, KnowledgeStage.DISABLED)


def stage_from_env(env: Mapping[str, str] | None = None) -> KnowledgeStage:
    """Read :data:`STAGE_ENV` from ``env`` (defaults to ``os.environ``)."""

    source = os.environ if env is None else env
    return resolve_stage(source.get(STAGE_ENV, ""))


def capture_enabled(stage: KnowledgeStage | None = None) -> bool:
    """Stage B+: attempt patches/evidence are captured (no prompt change)."""

    return resolve_stage(stage if stage is not None else stage_from_env()) >= (
        KnowledgeStage.SHADOW
    )


def recovery_enabled(stage: KnowledgeStage | None = None) -> bool:
    """Stage C+: retries receive the bounded recovery view."""

    return resolve_stage(stage if stage is not None else stage_from_env()) >= (
        KnowledgeStage.RECOVERY
    )


def graph_sharing_enabled(stage: KnowledgeStage | None = None) -> bool:
    """Stage D+: graph-scoped claims may be read across attempts."""

    return resolve_stage(stage if stage is not None else stage_from_env()) >= (
        KnowledgeStage.GRAPH
    )


def canonical_enabled(stage: KnowledgeStage | None = None) -> bool:
    """Stage E+: repository-canonical promotion is permitted."""

    return resolve_stage(stage if stage is not None else stage_from_env()) >= (
        KnowledgeStage.CANONICAL
    )


def describe_stage(stage: KnowledgeStage | None = None) -> dict:
    """Log-safe stage description carried in manifests/events."""

    resolved = resolve_stage(stage if stage is not None else stage_from_env())
    return {
        "stage": resolved.name.lower(),
        "capture": resolved >= KnowledgeStage.SHADOW,
        "recovery": resolved >= KnowledgeStage.RECOVERY,
        "graph": resolved >= KnowledgeStage.GRAPH,
        "canonical": resolved >= KnowledgeStage.CANONICAL,
    }


def _clamped_int(raw: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def recovery_budget_tokens(env: Mapping[str, str] | None = None) -> int:
    """Token budget of the retry recovery view (default 600, clamp 128..2000)."""

    source = os.environ if env is None else env
    return _clamped_int(
        source.get(RECOVERY_BUDGET_ENV, ""),
        default=DEFAULT_RECOVERY_BUDGET_TOKENS,
        minimum=128,
        maximum=2000,
    )


def retrieval_budget_tokens(env: Mapping[str, str] | None = None) -> int:
    """Token budget of the graph-knowledge read face (default 500, clamp 128..4000)."""

    source = os.environ if env is None else env
    return _clamped_int(
        source.get(RETRIEVAL_BUDGET_ENV, ""),
        default=DEFAULT_RETRIEVAL_BUDGET_TOKENS,
        minimum=128,
        maximum=4000,
    )


def chain_max_rounds(env: Mapping[str, str] | None = None) -> int:
    """How many prior attempt rounds recovery may consult (default 20)."""

    source = os.environ if env is None else env
    return _clamped_int(
        source.get(ATTEMPT_CHAIN_ROUNDS_ENV, ""),
        default=DEFAULT_ATTEMPT_CHAIN_MAX_ROUNDS,
        minimum=1,
        maximum=100,
    )


def max_patch_files(env: Mapping[str, str] | None = None) -> int:
    """How many changed files one captured patch round may carry (20)."""

    source = os.environ if env is None else env
    return _clamped_int(
        source.get(MAX_PATCH_FILES_ENV, ""),
        default=DEFAULT_MAX_PATCH_FILES,
        minimum=1,
        maximum=200,
    )


def max_patch_diff_chars(env: Mapping[str, str] | None = None) -> int:
    """Total diff-character ceiling for one captured patch round (20k)."""

    source = os.environ if env is None else env
    return _clamped_int(
        source.get(MAX_PATCH_DIFF_CHARS_ENV, ""),
        default=DEFAULT_MAX_PATCH_DIFF_CHARS,
        minimum=500,
        maximum=200_000,
    )
