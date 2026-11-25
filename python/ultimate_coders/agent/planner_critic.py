"""Plan Critic: deterministic exploration review before plan approval (v10 #23).

The Plan Critic is the cheap, non-LLM counterpart of the planning review: it
checks whether a candidate plan actually explored every area its spec says is
required, and whether those observations are backed by evidence.  It never
calls a model and never reads the repository -- the caller passes the spec and
the observations it already collected.

The review produces a :class:`PlanCritique` with two orthogonal coverage
metrics:

* ``exploration_precision`` -- the share of observations that are both
  relevant (in the spec's required set) and evidence-backed;
* ``exploration_recall`` -- the share of required areas covered by at least
  one observation.

``approved`` is conservative: the plan must meet the configured recall floor
and every covered required area must carry evidence.  ``corrections`` names
the concrete missing observations so a planning agent can repair the plan.
:func:`critic_prompt` renders the critique as a bounded (<=150 word) prompt
fragment.

Everything is opt-in through ``UC_PLANNER_CRITIC`` and pure at import time.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_MAX_CORRECTIONS",
    "DEFAULT_MIN_PRECISION",
    "DEFAULT_MIN_RECALL",
    "PLANNER_CRITIC_ENV",
    "PlanCritic",
    "PlanCritique",
    "critic_prompt",
    "planner_critic_enabled",
]

#: Environment flag that enables Plan Critic review.
PLANNER_CRITIC_ENV = "UC_PLANNER_CRITIC"
DEFAULT_MIN_RECALL = 0.8
DEFAULT_MIN_PRECISION = 0.5
DEFAULT_MAX_CORRECTIONS = 10
_MAX_PROMPT_WORDS = 150
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_TARGET_KEYS = ("target", "path", "file", "symbol", "name", "id")


def planner_critic_enabled() -> bool:
    """Return whether the Plan Critic is enabled via ``UC_PLANNER_CRITIC``."""

    return os.environ.get(PLANNER_CRITIC_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class PlanCritique:
    """Deterministic review of one plan's exploration coverage."""

    exploration_precision: float
    exploration_recall: float
    confidence: float
    corrections: tuple[str, ...]
    approved: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "exploration_precision": self.exploration_precision,
            "exploration_recall": self.exploration_recall,
            "confidence": self.confidence,
            "corrections": list(self.corrections),
            "approved": self.approved,
        }


@dataclass(frozen=True)
class _Observation:
    target: str
    evidence: bool


class PlanCritic:
    """Deterministic coverage/evidence heuristic reviewer."""

    def __init__(
        self,
        *,
        min_recall: float = DEFAULT_MIN_RECALL,
        min_precision: float = DEFAULT_MIN_PRECISION,
        max_corrections: int = DEFAULT_MAX_CORRECTIONS,
    ) -> None:
        if not 0.0 <= float(min_recall) <= 1.0:
            raise ValueError("min_recall must be between 0 and 1")
        if not 0.0 <= float(min_precision) <= 1.0:
            raise ValueError("min_precision must be between 0 and 1")
        if int(max_corrections) < 0:
            raise ValueError("max_corrections must be >= 0")
        self.min_recall = float(min_recall)
        self.min_precision = float(min_precision)
        self.max_corrections = int(max_corrections)

    def review(self, spec: Any, observations: Sequence[Any]) -> PlanCritique:
        """Review ``observations`` against the required areas in ``spec``.

        ``spec`` may be a mapping with ``required``/``required_targets``/
        ``required_exploration``/``targets`` (a sequence of names), a plain
        sequence of names, or a single string.  ``observations`` is a
        sequence of mappings (``{"target": ..., "evidence": [...],
        "verified": bool}``) or plain target strings.
        """

        required = _required_targets(spec)
        parsed = [_observation(item) for item in observations]
        required_set = set(required)
        observed: dict[str, bool] = {}
        for item in parsed:
            if not item.target:
                continue
            observed[item.target] = observed.get(item.target, False) or item.evidence

        covered = [target for target in required if target in observed]
        recall = len(covered) / len(required) if required else 1.0

        if parsed:
            relevant_evidenced = sum(
                1 for item in parsed if item.target in required_set and item.evidence
            )
            precision = relevant_evidenced / len(parsed)
        else:
            precision = 1.0 if not required else 0.0

        corrections: list[str] = []
        for target in required:
            if target not in observed:
                corrections.append(
                    f"Inspect `{target}` before approval: no observation covers this "
                    "required area."
                )
            elif not observed[target]:
                corrections.append(
                    f"Collect concrete evidence for `{target}`: the observation is not "
                    "evidence-backed."
                )
        if required:
            for item in parsed:
                if item.target and item.target not in required_set:
                    corrections.append(
                        f"Drop or justify `{item.target}`: it is outside the required "
                        "exploration scope."
                    )
                    break

        capped = tuple(corrections[: self.max_corrections])
        evidence_complete = all(observed.get(target, False) for target in covered)
        approved = (
            recall >= self.min_recall
            and precision >= self.min_precision
            and evidence_complete
        )
        confidence = round(0.7 * recall + 0.3 * precision, 6)
        return PlanCritique(
            exploration_precision=round(precision, 6),
            exploration_recall=round(recall, 6),
            confidence=confidence,
            corrections=capped,
            approved=approved,
        )

    def critic_prompt(self, critique: PlanCritique, *, max_words: int = _MAX_PROMPT_WORDS) -> str:
        """Render a bounded critique prompt (see :func:`critic_prompt`)."""

        return critic_prompt(critique, max_words=max_words)


def critic_prompt(critique: PlanCritique, *, max_words: int = _MAX_PROMPT_WORDS) -> str:
    """Render a PlanCritique as a bounded prompt fragment (<= ``max_words``).

    The prompt deliberately restates the evidence rule rather than asking the
    model to "be careful": an approving model must still show one concrete
    observation per required area.
    """

    if int(max_words) < 1:
        raise ValueError("max_words must be >= 1")
    status = "APPROVED" if critique.approved else "NOT APPROVED"
    parts = [
        f"Plan critic: {status}.",
        f"exploration_precision={critique.exploration_precision:.3f}",
        f"exploration_recall={critique.exploration_recall:.3f}",
        f"confidence={critique.confidence:.3f}.",
    ]
    if critique.corrections:
        parts.append("Corrections: " + " ".join(critique.corrections))
    parts.append(
        "Approve only when every required area has a concrete observation with evidence; "
        "otherwise request the missing observation instead of guessing."
    )
    words = " ".join(parts).split()
    return " ".join(words[: int(max_words)])


def _required_targets(spec: Any) -> list[str]:
    """Pull the required exploration targets out of a flexible spec shape."""

    raw: Any = ()
    if isinstance(spec, Mapping):
        for key in ("required", "required_targets", "required_exploration", "targets", "files"):
            if key in spec and spec[key] is not None:
                raw = spec[key]
                break
    elif isinstance(spec, str):
        raw = [spec]
    elif spec is not None:
        for attribute in (
            "required",
            "required_targets",
            "required_exploration",
            "targets",
            "target_files",
            "files",
        ):
            value = getattr(spec, attribute, None)
            if value is not None:
                raw = value
                break
        else:
            raw = spec

    targets: list[str] = []
    seen: set[str] = set()
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    try:
        iterator = iter(raw)
    except TypeError:
        iterator = iter(())
    for value in iterator:
        target = _target_name(value)
        if target and target not in seen:
            targets.append(target)
            seen.add(target)
    return targets


def _target_name(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in _TARGET_KEYS:
            if value.get(key) is not None and str(value[key]).strip():
                return str(value[key]).strip()
        return ""
    for attribute in ("path", "target", "file", "symbol", "name", "id"):
        attr_value = getattr(value, attribute, None)
        if attr_value is not None and str(attr_value).strip():
            return str(attr_value).strip()
    return str(value or "").strip()


def _observation(value: Any) -> _Observation:
    if isinstance(value, Mapping):
        target = _target_name(value)
        evidence = _observation_evidence(value)
        return _Observation(target=target, evidence=evidence)
    target = _target_name(value)
    evidence = bool(target)
    verified = getattr(value, "verified", None)
    if verified is False:
        return _Observation(target=target, evidence=False)
    evidence_value = getattr(value, "evidence", None)
    if evidence_value is not None:
        evidence = _evidence_value_passes(evidence_value)
    return _Observation(target=target, evidence=evidence)


def _evidence_value_passes(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, frozenset, Mapping)):
        return len(value) > 0
    return bool(value)


def _observation_evidence(value: Mapping[str, Any]) -> bool:
    """Decide whether one observation carries usable evidence.

    A plain mapping without an explicit ``evidence``/``verified`` marker is
    treated as evidence-backed because the observation itself is the result.
    An explicitly empty evidence collection or ``verified=False`` makes the
    observation bare.
    """

    if "verified" in value and value["verified"] is False:
        return False
    if "evidence" not in value:
        return True
    return _evidence_value_passes(value["evidence"])


def _iter_observations(values: Iterable[Any]) -> list[_Observation]:
    return [_observation(value) for value in values]
