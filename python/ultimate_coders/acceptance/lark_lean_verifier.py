"""Lean 4 segmented equivalence verification for GPU kernels (ledger item #86).

The verifier models the LARK shape: a machine model is formalized once, the
agent decomposes a kernel into segments, each segment is checked for syntactic
validity, and each segment must then carry a proof obligation that relates it
to the high-level PyTorch semantics. This module is deterministic and does not
invoke Lean, CUDA, Triton, or a model; callers provide the proof outcomes that
the external toolchain produced.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

__all__ = [
    "LARK_LEAN_VERIFIER_ENV",
    "LarkLeanError",
    "KernelLevel",
    "ProofStatus",
    "MachineModel",
    "KernelSegment",
    "SegmentProof",
    "KernelProofReport",
    "LarkLeanVerifier",
    "lark_lean_verifier_enabled",
]

LARK_LEAN_VERIFIER_ENV = "UC_LARK_LEAN_VERIFIER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def lark_lean_verifier_enabled() -> bool:
    return os.environ.get(LARK_LEAN_VERIFIER_ENV, "").strip().lower() in _TRUTHY


class LarkLeanError(ValueError):
    """Raised when a kernel proof request is malformed."""


@dataclass(frozen=True)
class KernelLevel:
    """One published LARK validation level."""

    name: str
    requires_global_equivalence: bool

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise LarkLeanError("level name must not be empty")


LEVEL_1 = KernelLevel("level-1", False)
LEVEL_2 = KernelLevel("level-2", True)


@dataclass(frozen=True)
class ProofStatus:
    value: str

    def __post_init__(self) -> None:
        allowed = {"proved", "failed", "unproved", "skipped"}
        if self.value not in allowed:
            raise LarkLeanError(f"unknown proof status: {self.value}")

    @property
    def proved(self) -> bool:
        return self.value == "proved"


PROVED = ProofStatus("proved")
FAILED = ProofStatus("failed")
UNPROVED = ProofStatus("unproved")
SKIPPED = ProofStatus("skipped")


@dataclass(frozen=True)
class MachineModel:
    language: str
    formalization: str
    operators: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.language.strip():
            raise LarkLeanError("machine language must not be empty")
        if not self.formalization.strip():
            raise LarkLeanError("formalization must not be empty")


@dataclass(frozen=True)
class KernelSegment:
    name: str
    source: str
    postconditions: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise LarkLeanError("segment name must not be empty")
        if not self.source.strip():
            raise LarkLeanError("segment source must not be empty")
        if not self.postconditions:
            raise LarkLeanError("segment needs at least one postcondition")


@dataclass(frozen=True)
class SegmentProof:
    segment: str
    status: ProofStatus
    obligations: tuple[str, ...] = ()
    detail: str = ""

    @property
    def proved(self) -> bool:
        return self.status.proved


@dataclass(frozen=True)
class KernelProofReport:
    level: KernelLevel
    segments: tuple[SegmentProof, ...]
    machine_model: str
    global_equivalence: bool
    race_checks: tuple[str, ...] = ()
    verified: bool = False

    @property
    def proved_segments(self) -> int:
        return sum(1 for item in self.segments if item.proved)

    @property
    def all_segments_proved(self) -> bool:
        return bool(self.segments) and all(item.proved for item in self.segments)

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level.name,
            "machine_model": self.machine_model,
            "segments": [item.segment for item in self.segments],
            "proved_segments": self.proved_segments,
            "all_segments_proved": self.all_segments_proved,
            "global_equivalence": self.global_equivalence,
            "race_checks": list(self.race_checks),
            "verified": self.verified,
        }


@dataclass
class LarkLeanVerifier:
    """Collects a machine model and verifies segmented kernel proofs."""

    model: MachineModel | None = None
    level: KernelLevel = field(default_factory=lambda: LEVEL_1)
    _proofs: dict[str, SegmentProof] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.level, KernelLevel):
            raise LarkLeanError("level must be a KernelLevel")

    def register_machine_model(self, model: MachineModel) -> None:
        if not isinstance(model, MachineModel):
            raise LarkLeanError("model must be a MachineModel")
        self.model = model

    def record_proof(self, proof: SegmentProof) -> None:
        if not isinstance(proof, SegmentProof):
            raise LarkLeanError("proof must be a SegmentProof")
        if proof.segment in self._proofs:
            raise LarkLeanError(f"duplicate proof for segment: {proof.segment}")
        self._proofs[proof.segment] = proof

    def verify(
        self,
        segments: tuple[KernelSegment, ...],
        *,
        global_equivalence: bool = False,
        race_checks: tuple[str, ...] = (),
    ) -> KernelProofReport:
        if self.model is None:
            raise LarkLeanError("a machine model must be registered first")
        if not segments:
            raise LarkLeanError("kernel must contain at least one segment")
        names = [segment.name for segment in segments]
        if len(names) != len(set(names)):
            raise LarkLeanError("segment names must be unique")
        known = set(names)
        for segment in segments:
            missing = [dependency for dependency in segment.depends_on if dependency not in known]
            if missing:
                raise LarkLeanError(f"unknown segment dependency: {missing[0]}")
        proofs: list[SegmentProof] = []
        for segment in segments:
            proof = self._proofs.get(segment.name, SegmentProof(segment.name, UNPROVED))
            if proof.status.proved and not proof.obligations:
                proof = SegmentProof(segment.name, UNPROVED, proof.obligations, "no obligations")
            proofs.append(proof)
        all_segments_proved = all(item.proved for item in proofs)
        verified = all_segments_proved and (
            not self.level.requires_global_equivalence or global_equivalence
        )
        return KernelProofReport(
            level=self.level,
            segments=tuple(proofs),
            machine_model=self.model.formalization,
            global_equivalence=global_equivalence,
            race_checks=tuple(race_checks),
            verified=verified,
        )

    def reset(self) -> None:
        self._proofs.clear()
