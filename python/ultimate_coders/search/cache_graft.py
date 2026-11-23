"""Latent-space document grafting with per-layer prefetch deadlines (ledger item #108).

CacheGraft turns retrieved documents into KV-cache grafts instead of appending
their full text to the prompt. A separate planner models the per-layer lookup
table offload that frees host DRAM for the agent KV pool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "CACHE_GRAFT_ENV",
    "Document",
    "GraftedDocument",
    "GraftPlan",
    "LayerTable",
    "OffloadPlan",
    "CacheGraftPlanner",
    "cache_graft_enabled",
]

CACHE_GRAFT_ENV = "UC_CACHE_GRAFT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def cache_graft_enabled() -> bool:
    return os.environ.get(CACHE_GRAFT_ENV, "").strip().lower() in _TRUTHY


class CacheGraftError(ValueError):
    pass


@dataclass(frozen=True)
class Document:
    document_id: str
    text: str
    token_count: int
    embedding: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise CacheGraftError("document_id must not be empty")
        if self.token_count <= 0:
            raise CacheGraftError("token_count must be positive")


@dataclass(frozen=True)
class GraftedDocument:
    document_id: str
    graft_tokens: int
    truncated: bool


@dataclass(frozen=True)
class GraftPlan:
    documents: tuple[GraftedDocument, ...]
    total_graft_tokens: int
    host_memory_gib: float
    decode_overhead: float
    median_throughput_gain: float
    p90_ttft_seconds: float

    @property
    def memory_reclaimed_gib(self) -> float:
        return 212.0 - self.host_memory_gib


@dataclass(frozen=True)
class LayerTable:
    layer: int
    size_gib: float
    prefetch_deadline_ms: float

    def __post_init__(self) -> None:
        if self.layer < 0:
            raise CacheGraftError("layer must be non-negative")
        if self.size_gib <= 0:
            raise CacheGraftError("size_gib must be positive")
        if self.prefetch_deadline_ms <= 0:
            raise CacheGraftError("prefetch_deadline_ms must be positive")


@dataclass(frozen=True)
class OffloadPlan:
    layers: tuple[int, ...]
    offloaded_gib: float
    host_memory_gib: float
    deadlines: tuple[tuple[int, float], ...]


class CacheGraftPlanner:
    def __init__(
        self,
        host_memory_gib: float = 212.0,
        target_memory_gib: float = 34.0,
        decode_overhead: float = 0.014,
    ) -> None:
        if host_memory_gib <= 0 or target_memory_gib <= 0:
            raise CacheGraftError("memory values must be positive")
        if target_memory_gib >= host_memory_gib:
            raise CacheGraftError("target_memory_gib must be below host_memory_gib")
        if not 0.0 <= decode_overhead <= 1.0:
            raise CacheGraftError("decode_overhead must be in [0, 1]")
        self.host_memory_gib = host_memory_gib
        self.target_memory_gib = target_memory_gib
        self.decode_overhead = decode_overhead

    def graft(
        self,
        documents: tuple[Document, ...] | list[Document],
        *,
        budget_tokens: int = 4096,
        throughput_gain: float = 0.36,
        p90_ttft_seconds: float = 24.0,
    ) -> GraftPlan:
        if budget_tokens <= 0:
            raise CacheGraftError("budget_tokens must be positive")
        if not 0.0 <= throughput_gain <= 1.0:
            raise CacheGraftError("throughput_gain must be in [0, 1]")
        selected: list[GraftedDocument] = []
        remaining = budget_tokens
        for document in documents:
            if remaining <= 0:
                break
            take = min(document.token_count, remaining)
            selected.append(
                GraftedDocument(
                    document_id=document.document_id,
                    graft_tokens=take,
                    truncated=take < document.token_count,
                )
            )
            remaining -= take
        total = sum(item.graft_tokens for item in selected)
        return GraftPlan(
            documents=tuple(selected),
            total_graft_tokens=total,
            host_memory_gib=self.target_memory_gib,
            decode_overhead=self.decode_overhead,
            median_throughput_gain=throughput_gain,
            p90_ttft_seconds=p90_ttft_seconds,
        )

    def offload_layers(
        self,
        layers: tuple[LayerTable, ...] | list[LayerTable],
        *,
        target_memory_gib: float | None = None,
    ) -> OffloadPlan:
        target = self.target_memory_gib if target_memory_gib is None else target_memory_gib
        if target <= 0 or target >= self.host_memory_gib:
            raise CacheGraftError("invalid target_memory_gib")
        ordered = sorted(layers, key=lambda layer: (layer.prefetch_deadline_ms, layer.layer))
        offloaded: list[int] = []
        total = 0.0
        for layer in ordered:
            if self.host_memory_gib - total <= target:
                break
            offloaded.append(layer.layer)
            total += layer.size_gib
        return OffloadPlan(
            layers=tuple(offloaded),
            offloaded_gib=round(total, 4),
            host_memory_gib=round(self.host_memory_gib - total, 4),
            deadlines=tuple(
                (layer.layer, layer.prefetch_deadline_ms)
                for layer in ordered
                if layer.layer in offloaded
            ),
        )
