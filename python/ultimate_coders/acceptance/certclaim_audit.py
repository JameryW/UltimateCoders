"""CertClaim-style model certification and cross-agent formal auditing (V28 #132).

A provider's model claim is only as good as the deployment on which it was
measured.  ``CertClaimAuditor`` replaces a single static benchmark number with
a multi-channel calibration over an authorised deployment set and reports a
simple misclassification bound.  ``CrossAgentFormalAuditor`` adds the
structured check that a Dafny proof actually establishes the specification,
rather than an implementation calling the specification predicate directly;
and ``CriteriaExposureGuard`` keeps hidden acceptance criteria out of the
agent's context.

The module is pure bookkeeping: callers supply measured channel results and
proof evidence.  It does not call a provider or a verifier.
``UC_CERTCLAIM_AUDIT`` is the opt-in flag.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

__all__ = [
    "CERTCLAIM_AUDIT_ENV",
    "ChannelMeasurement",
    "ClaimCertificate",
    "CertClaimAuditor",
    "FormalAuditRecord",
    "FormalAuditReport",
    "CrossAgentFormalAuditor",
    "EvaluationCriteria",
    "CriteriaExposureGuard",
    "certclaim_audit_enabled",
]

CERTCLAIM_AUDIT_ENV = "UC_CERTCLAIM_AUDIT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def certclaim_audit_enabled() -> bool:
    """Return whether certification auditing is enabled."""

    return os.environ.get(CERTCLAIM_AUDIT_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ChannelMeasurement:
    """One calibration channel for a deployment."""

    name: str
    expected: float
    observed: float
    tolerance: float = 0.05
    samples: int = 1

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("channel name must not be empty")
        if self.tolerance < 0:
            raise ValueError("tolerance must not be negative")
        if self.samples <= 0:
            raise ValueError("samples must be positive")
        if not math.isfinite(self.expected) or not math.isfinite(self.observed):
            raise ValueError("channel values must be finite")

    @property
    def drift(self) -> float:
        return abs(self.observed - self.expected)

    @property
    def passed(self) -> bool:
        return self.drift <= self.tolerance


@dataclass(frozen=True)
class ClaimCertificate:
    """The audit result for one provider deployment."""

    provider: str
    model: str
    deployment: str
    accepted: bool
    channels: tuple[ChannelMeasurement, ...]
    error_bound: float
    reasons: tuple[str, ...]

    @property
    def max_drift(self) -> float:
        return max((c.drift for c in self.channels), default=0.0)

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "deployment": self.deployment,
            "accepted": self.accepted,
            "error_bound": round(self.error_bound, 4),
            "max_drift": round(self.max_drift, 4),
            "reasons": list(self.reasons),
        }


class CertClaimAuditor:
    """Multi-channel certification over an authorised deployment set."""

    def audit(
        self,
        *,
        provider: str,
        model: str,
        deployment: str,
        authorized_deployments: Iterable[str],
        channels: Iterable[ChannelMeasurement],
        required_channels: int = 2,
        error_bound_limit: float = 0.1,
    ) -> ClaimCertificate:
        if not provider.strip() or not model.strip() or not deployment.strip():
            raise ValueError("provider, model, and deployment must not be empty")
        if required_channels <= 0:
            raise ValueError("required_channels must be positive")
        if error_bound_limit < 0:
            raise ValueError("error_bound_limit must not be negative")
        allowed = {d for d in authorized_deployments if d.strip()}
        ordered = tuple(channels)
        if not ordered:
            raise ValueError("at least one channel is required")
        if len({c.name for c in ordered}) != len(ordered):
            raise ValueError("duplicate channel")

        reasons: list[str] = []
        if deployment not in allowed:
            reasons.append("deployment_not_authorized")
        if len(ordered) < required_channels:
            reasons.append("insufficient_channels")
        failed = [c.name for c in ordered if not c.passed]
        if failed:
            reasons.append("channel_drift:" + ",".join(failed))

        # Independent channel errors are bounded by their mean; the sampling
        # term makes the bound less confident when few observations exist.
        mean_drift = sum(c.drift for c in ordered) / len(ordered)
        samples = sum(c.samples for c in ordered)
        sampling = 1.0 / math.sqrt(max(1, samples))
        error_bound = mean_drift + sampling / max(1, len(ordered))
        if error_bound > error_bound_limit:
            reasons.append("error_bound_exceeded")

        accepted = not reasons
        return ClaimCertificate(
            provider=provider,
            model=model,
            deployment=deployment,
            accepted=accepted,
            channels=ordered,
            error_bound=error_bound,
            reasons=tuple(reasons),
        )

    def channels_by_name(
        self, certificate: ClaimCertificate
    ) -> Mapping[str, ChannelMeasurement]:
        return {c.name: c for c in certificate.channels}


@dataclass(frozen=True)
class FormalAuditRecord:
    """One cross-agent formal-verification result."""

    implementer: str
    verifier: str
    spec_id: str
    verified: bool
    direct_spec_predicate_calls: int = 0
    semantic_match: bool = False
    reextracted_spec_match: bool = False

    def __post_init__(self) -> None:
        if not self.implementer.strip() or not self.verifier.strip():
            raise ValueError("implementer and verifier must not be empty")
        if not self.spec_id.strip():
            raise ValueError("spec_id must not be empty")
        if self.direct_spec_predicate_calls < 0:
            raise ValueError("direct_spec_predicate_calls must not be negative")


@dataclass(frozen=True)
class FormalAuditReport:
    """Structured verdict for a set of formal audits."""

    records: tuple[FormalAuditRecord, ...]
    trustworthy: tuple[FormalAuditRecord, ...]
    rejected: tuple[FormalAuditRecord, ...]
    consistency: float

    @property
    def all_trustworthy(self) -> bool:
        return not self.rejected

    def to_dict(self) -> dict[str, object]:
        return {
            "trustworthy": [r.spec_id for r in self.trustworthy],
            "rejected": [r.spec_id for r in self.rejected],
            "consistency": round(self.consistency, 4),
        }


class CrossAgentFormalAuditor:
    """Reject proofs that are formally green but semantically vacuous."""

    def audit(self, records: Iterable[FormalAuditRecord]) -> FormalAuditReport:
        ordered = tuple(records)
        if not ordered:
            raise ValueError("at least one formal audit record is required")
        if len({r.spec_id for r in ordered}) != len(ordered):
            raise ValueError("duplicate spec_id")

        trustworthy: list[FormalAuditRecord] = []
        rejected: list[FormalAuditRecord] = []
        for record in ordered:
            if (
                record.verified
                and record.direct_spec_predicate_calls == 0
                and record.semantic_match
                and record.reextracted_spec_match
            ):
                trustworthy.append(record)
            else:
                rejected.append(record)

        consistency = len(trustworthy) / len(ordered)
        return FormalAuditReport(ordered, tuple(trustworthy), tuple(rejected), consistency)


@dataclass(frozen=True)
class EvaluationCriteria:
    """Visibility state of the grader's acceptance criteria."""

    name: str
    hidden: bool = True
    exposed_to_agent: bool = False
    version: str = "v1"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("criteria name must not be empty")


class CriteriaExposureGuard:
    """Keep hidden grading criteria out of the agent context."""

    def safe(self, criteria: Iterable[EvaluationCriteria]) -> bool:
        return all(not c.exposed_to_agent for c in criteria if c.hidden)

    def exposed(self, criteria: Iterable[EvaluationCriteria]) -> tuple[str, ...]:
        return tuple(c.name for c in criteria if c.hidden and c.exposed_to_agent)
