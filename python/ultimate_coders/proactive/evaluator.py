"""Rule-based proactive evaluation: facts in, evidence-backed proposals out.

Invariant: **a proposal is a suggestion with receipts**. Every proposal this
module emits carries at least one stable fact id -- ``ci:run-42``,
``stall:task-1:2``, ``alert:7``, ``followup:<id>``, ``debt:<id>`` -- naming the
evidence that produced it. A candidate whose evidence would be empty is refused
and reported as a :class:`Refusal`, never emitted: an unexplainable suggestion is
not auditable, so it must not exist.

What this module does *not* do: it does not admit, budget, persist or execute a
proposal. Those decisions belong to the Rust control plane, whose
``proactive.proposal.submit`` gate can still refuse anything produced here. The
evaluator answers "what is worth doing, and why"; it never answers "may I".

Determinism: the rules are pure functions of the context mapping -- no clock, no
randomness, no network, no model call. The same context produces the same
proposals in the same order, so an evaluation can be replayed from an event log
and asserted in tests. The one documented exception is the optional injectable
``judge``: it may only *refine confidence* (it returns a number, it cannot return
a proposal), and a judge that raises or returns garbage leaves the rule
confidence untouched rather than failing the evaluation.

Honest limitations:

* The rule set is deliberately small and conservative: a failing CI run, a
  stalled task, a high-severity alert, a declared follow-up and declared
  maintenance debt. Anything richer is a judgement this layer cannot audit.
* ``confidence`` is an ordering hint for a review queue, not a probability, and
  the gateway never grants execution from it.
* ``estimated_tokens`` is a fixed per-kind prior used only to give the gateway a
  number to check ``max_tokens_per_action`` against; it is not a reservation.
* An exhausted task budget (``BudgetVerdict.admitted is False``) suppresses the
  whole evaluation: proposing more work with no budget left is the exact failure
  the budget ledger exists to prevent.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
from enum import Enum
from typing import Any

from ultimate_coders.proactive.modes import ProactiveMode, proactive_mode_from_env
from ultimate_coders.proactive.watchdog_input import progress_stall_limit

logger = logging.getLogger(__name__)

#: Maximum proposals a single rule may contribute in one evaluation. Keeps a
#: noisy context (twenty alerts) from flooding the review queue; the gateway
#: additionally caps open proposals.
MAX_PROPOSALS_PER_RULE = 3

#: CI conclusions that mean "this run failed" (``cancelled`` is not a defect).
_FAILING_CI_CONCLUSIONS = frozenset({"failure", "timed_out"})


class ProposalKind(str, Enum):
    """What kind of work a proposal suggests. Values are the gateway tokens."""

    DISCOVERED_FOLLOWUP = "discovered_followup"
    UNBLOCK_STALLED = "unblock_stalled"
    FIX_FAILING_CI = "fix_failing_ci"
    MAINTENANCE_DEBT = "maintenance_debt"
    INVESTIGATE_ALERT = "investigate_alert"


#: Every valid ``kind`` token, for request validation.
PROPOSAL_KIND_TOKENS: tuple[str, ...] = tuple(kind.value for kind in ProposalKind)

#: Advisory token estimate per kind (not a reservation).
ESTIMATED_TOKENS: Mapping[ProposalKind, int] = {
    ProposalKind.FIX_FAILING_CI: 40_000,
    ProposalKind.UNBLOCK_STALLED: 25_000,
    ProposalKind.DISCOVERED_FOLLOWUP: 20_000,
    ProposalKind.INVESTIGATE_ALERT: 15_000,
    ProposalKind.MAINTENANCE_DEBT: 30_000,
}

#: Rule priors for the review queue's ordering (not probabilities).
BASE_CONFIDENCE: Mapping[ProposalKind, float] = {
    ProposalKind.FIX_FAILING_CI: 0.6,
    ProposalKind.UNBLOCK_STALLED: 0.55,
    ProposalKind.DISCOVERED_FOLLOWUP: 0.45,
    ProposalKind.MAINTENANCE_DEBT: 0.3,
    ProposalKind.INVESTIGATE_ALERT: 0.4,
}

#: Alert severity priors for ``investigate_alert``.
ALERT_CONFIDENCE: Mapping[str, float] = {
    "critical": 0.8,
    "high": 0.65,
    "medium": 0.5,
    "low": 0.35,
}


@dataclass(frozen=True)
class Proposal:
    """One evidence-backed suggestion. A suggestion, not an authorization."""

    kind: ProposalKind
    title: str
    rationale: str
    confidence: float
    evidence: tuple[str, ...] = ()
    proposed_request: str | None = None
    project_id: str | None = None
    estimated_tokens: int | None = None
    case_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Wire shape of the proposal fields (optional keys omitted when unset)."""
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "title": self.title,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }
        if self.case_id is not None:
            payload["case_id"] = self.case_id
        if self.proposed_request is not None:
            payload["proposed_request"] = self.proposed_request
        if self.project_id is not None:
            payload["project_id"] = self.project_id
        if self.estimated_tokens is not None:
            payload["estimated_tokens"] = self.estimated_tokens
        return payload

    def to_submit_payload(self, *, principal_kind: str, principal_id: str) -> dict[str, Any]:
        """The exact ``proactive.proposal.submit`` payload for this proposal.

        The principal is required rather than defaulted: the gateway authorizes
        the caller, and an anonymous default would attribute a suggestion to
        nobody. Admission is still the gateway's answer, not ours.
        """
        if not principal_kind.strip() or not principal_id.strip():
            raise ValueError("principal_kind and principal_id are required")
        return {
            **self.to_dict(),
            "principal_kind": principal_kind,
            "principal_id": principal_id,
        }


#: An injectable confidence judge: ``(proposal, context) -> confidence``.
#: Returning ``None`` (or anything unusable) keeps the rule's own confidence.
Judge = Callable[["Proposal", Mapping[str, Any]], Any]


@dataclass(frozen=True)
class Refusal:
    """A candidate the evaluator declined to emit, with the reason on record."""

    reason: str
    kind: str = ""
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "kind": self.kind, "title": self.title}


@dataclass(frozen=True)
class EvaluationResult:
    """One evaluation: what was suggested, what was refused, and on what facts.

    ``proposals`` are actionable suggestions (``propose``/``execute`` mode);
    ``observed`` are the same suggestions recorded but not surfaced
    (``observe`` mode). ``evidence_ids`` is the auditable trail: the union of
    every emitted proposal's fact ids, in stable first-seen order, so a false
    trigger can be traced to the fact that fired it.
    """

    mode: ProactiveMode
    evaluated: bool
    observed: tuple[Proposal, ...] = ()
    proposals: tuple[Proposal, ...] = ()
    refusals: tuple[Refusal, ...] = ()
    blocked_by: str | None = None

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        """Every fact id the emitted proposals rest on (deduped, stable order)."""
        seen: list[str] = []
        for proposal in self.proposals + self.observed:
            for evidence in proposal.evidence:
                if evidence not in seen:
                    seen.append(evidence)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": self.mode.value,
            "evaluated": self.evaluated,
            "proposals": [proposal.to_dict() for proposal in self.proposals],
            "observed": [proposal.to_dict() for proposal in self.observed],
            "refusals": [refusal.to_dict() for refusal in self.refusals],
            "evidence_ids": list(self.evidence_ids),
        }
        if self.blocked_by:
            payload["blocked_by"] = self.blocked_by
        return payload


@dataclass(frozen=True)
class _Ambient:
    """Facts shared by every rule: the case, task and project in scope."""

    case_id: str | None
    task_id: str
    task_status: str
    project_id: str | None


def _mapping(value: Any) -> Mapping[str, Any]:
    """Best-effort mapping view of a context fragment.

    Accepts a mapping, or a dataclass instance (``ProgressVerdict``,
    ``BudgetVerdict``) so a caller can pass the object it already has instead of
    hand-serializing it. Anything else is "absent", never a crash: a missing
    fragment means the rule has no evidence and emits nothing.
    """
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return {}


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _clamp_confidence(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, round(float(value), 4)))


def _confidence_from(raw: Any) -> float | None:
    """Extract a confidence from a judge answer (``float`` or ``{"confidence": x}``)."""
    if isinstance(raw, Mapping):
        raw = raw.get("confidence")
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return _clamp_confidence(float(raw))


def _budget_exhausted(budget: Mapping[str, Any]) -> bool:
    """Only an explicit ``admitted=False`` blocks; a missing budget is unknown."""
    return budget.get("admitted") is False


class ProactiveEvaluator:
    """Deterministic rule engine with an optional confidence-only judge.

    ``stall_limit`` mirrors ``UC_PROGRESS_STALL_LIMIT`` (default 2, the progress
    detector's threshold): below it a task is still making progress and the
    ``unblock_stalled`` rule stays quiet.
    """

    def __init__(self, *, judge: Judge | None = None, stall_limit: int | None = None) -> None:
        self._judge = judge
        self.stall_limit = (
            max(1, stall_limit) if stall_limit is not None else progress_stall_limit()
        )

    # ── evaluation ───────────────────────────────────────────────

    def evaluate(
        self,
        context: Mapping[str, Any],
        *,
        mode: ProactiveMode | None = None,
    ) -> EvaluationResult:
        """Evaluate one context and return what may be proposed, and why.

        ``mode`` defaults to ``UC_PROACTIVE_MODE`` (``off`` when unset), so the
        default call evaluates nothing at all. In ``off`` mode the context is not
        even inspected; in ``observe`` mode the suggestions are returned as
        ``observed`` and never as ``proposals``.
        """
        if not isinstance(context, Mapping):
            raise TypeError(f"proactive context must be a mapping, got {type(context).__name__}")
        active = mode if mode is not None else proactive_mode_from_env()
        if not active.evaluates():
            return EvaluationResult(mode=active, evaluated=False)
        if _budget_exhausted(_mapping(context.get("budget"))):
            logger.info("proactive evaluation suppressed: task budget exhausted")
            return EvaluationResult(mode=active, evaluated=True, blocked_by="budget_exhausted")

        admitted, refusals = self._admit(self._candidates(context))
        if self._judge is not None:
            admitted = [self._refine(proposal, context) for proposal in admitted]
        if active is ProactiveMode.OBSERVE:
            return EvaluationResult(
                mode=active,
                evaluated=True,
                observed=tuple(admitted),
                refusals=tuple(refusals),
            )
        return EvaluationResult(
            mode=active,
            evaluated=True,
            proposals=tuple(admitted),
            refusals=tuple(refusals),
        )

    # ── rules ────────────────────────────────────────────────────

    def _candidates(self, context: Mapping[str, Any]) -> list[Proposal]:
        case = _mapping(context.get("case"))
        task = _mapping(context.get("task"))
        ambient = _Ambient(
            case_id=_text(case.get("id")) or None,
            task_id=_text(task.get("id")),
            task_status=_text(task.get("status")),
            project_id=_text(task.get("project_id")) or None,
        )
        candidates: list[Proposal] = []
        candidates.extend(self._ci_candidates(context, ambient))
        candidates.extend(self._stall_candidates(context, ambient))
        candidates.extend(self._alert_candidates(context, ambient))
        candidates.extend(self._followup_candidates(context, ambient))
        candidates.extend(self._maintenance_candidates(context, ambient))
        return candidates

    def _ci_candidates(self, context: Mapping[str, Any], ambient: _Ambient) -> list[Proposal]:
        ci = _mapping(context.get("ci"))
        conclusion = _text(ci.get("conclusion")).lower()
        if conclusion not in _FAILING_CI_CONCLUSIONS:
            return []
        run_id = _text(ci.get("run_id"))
        workflow = _text(ci.get("workflow"))
        head_sha = _text(ci.get("head_sha"))
        project_id = _text(ci.get("project_id")) or ambient.project_id
        facts = [f"CI run {run_id or '<unnamed>'} concluded {conclusion}"]
        if workflow:
            facts.append(f"workflow {workflow}")
        if head_sha:
            facts.append(f"head {head_sha}")
        if ambient.task_id:
            facts.append(f"task {ambient.task_id} was {ambient.task_status or 'in progress'}")
        rationale = (
            "; ".join(facts)
            + ". A failing CI run is a concrete, checkable defect: propose one bounded fix "
            "attempt instead of repeating the same contract."
        )
        title = f"Fix failing CI run {run_id}".strip()
        request = f"Investigate and fix the failing CI run {run_id}".strip()
        if project_id:
            request = f"{request} in project {project_id}"
        return [
            Proposal(
                kind=ProposalKind.FIX_FAILING_CI,
                title=title,
                rationale=rationale,
                confidence=BASE_CONFIDENCE[ProposalKind.FIX_FAILING_CI],
                evidence=(f"ci:{run_id}",) if run_id else (),
                proposed_request=request + ".",
                project_id=project_id,
                estimated_tokens=ESTIMATED_TOKENS[ProposalKind.FIX_FAILING_CI],
                case_id=ambient.case_id,
            )
        ]

    def _stall_candidates(self, context: Mapping[str, Any], ambient: _Ambient) -> list[Proposal]:
        progress = _mapping(context.get("progress"))
        action = _text(progress.get("action"))
        streak = _count(progress.get("stalled_streak"))
        if action != "stop" and streak < self.stall_limit:
            return []
        subtask_id = _text(progress.get("subtask_id"))
        where = (
            f"subtask {subtask_id} of task {ambient.task_id}"
            if subtask_id and ambient.task_id
            else f"task {ambient.task_id or '<unnamed>'}"
        )
        rationale = (
            f"{where} produced no new evidence for {streak} consecutive attempt(s): the "
            "normalized error signature, stderr and file set were unchanged. Another identical "
            "retry burns budget; propose a bounded unblock instead."
        )
        title = f"Unblock stalled task {ambient.task_id}".strip()
        request = f"Unblock the stalled task {ambient.task_id}".strip()
        return [
            Proposal(
                kind=ProposalKind.UNBLOCK_STALLED,
                title=title,
                rationale=rationale,
                confidence=BASE_CONFIDENCE[ProposalKind.UNBLOCK_STALLED],
                evidence=(
                    (f"stall:{ambient.task_id}:{streak}",) if ambient.task_id and streak else ()
                ),
                proposed_request=request + ".",
                project_id=ambient.project_id,
                estimated_tokens=ESTIMATED_TOKENS[ProposalKind.UNBLOCK_STALLED],
                case_id=ambient.case_id,
            )
        ]

    def _alert_candidates(self, context: Mapping[str, Any], ambient: _Ambient) -> list[Proposal]:
        proposals: list[Proposal] = []
        for entry in _sequence(context.get("alerts")):
            alert = _mapping(entry)
            if not alert:
                continue
            severity = _text(alert.get("severity")).lower()
            alert_id = _text(alert.get("id"))
            summary = _text(alert.get("summary")) or _text(alert.get("title"))
            label = f"{severity} alert" if severity else "alert"
            rationale = (
                f"{label.capitalize()} {alert_id or '<unnamed>'} reports "
                f"{summary or 'an unspecified regression'}; propose one bounded investigation "
                "before the alert is dismissed."
            )
            request = f"Investigate alert {alert_id}".strip()
            if summary:
                request = f"{request}: {summary}"
            proposals.append(
                Proposal(
                    kind=ProposalKind.INVESTIGATE_ALERT,
                    title=f"Investigate {label} {alert_id}".strip(),
                    rationale=rationale,
                    confidence=ALERT_CONFIDENCE.get(
                        severity, BASE_CONFIDENCE[ProposalKind.INVESTIGATE_ALERT]
                    ),
                    evidence=(f"alert:{alert_id}",) if alert_id else (),
                    proposed_request=request + ".",
                    project_id=_text(alert.get("project_id")) or ambient.project_id,
                    estimated_tokens=ESTIMATED_TOKENS[ProposalKind.INVESTIGATE_ALERT],
                    case_id=ambient.case_id,
                )
            )
            if len(proposals) >= MAX_PROPOSALS_PER_RULE:
                break
        return proposals

    def _followup_candidates(self, context: Mapping[str, Any], ambient: _Ambient) -> list[Proposal]:
        proposals: list[Proposal] = []
        for entry in _sequence(context.get("followups")):
            followup = _mapping(entry)
            if not followup:
                continue
            followup_id = _text(followup.get("id"))
            subject = _text(followup.get("title")) or _text(followup.get("summary"))
            detail = _text(followup.get("detail"))
            rationale = (
                f"Work on case {ambient.case_id or '<unnamed>'} surfaced follow-up "
                f"{followup_id or '<unnamed>'}"
                + (f": {detail}" if detail else "")
                + ". It is outside the current contract, so it is proposed as its own case "
                "rather than folded into a running one."
            )
            request = _text(followup.get("request")) or (
                "Implement the follow-up discovered during the current case: "
                f"{subject or followup_id or 'unspecified'}."
            )
            proposals.append(
                Proposal(
                    kind=ProposalKind.DISCOVERED_FOLLOWUP,
                    title=f"Follow up: {subject or followup_id or 'discovered work'}",
                    rationale=rationale,
                    confidence=BASE_CONFIDENCE[ProposalKind.DISCOVERED_FOLLOWUP],
                    evidence=(f"followup:{followup_id}",) if followup_id else (),
                    proposed_request=request,
                    project_id=_text(followup.get("project_id")) or ambient.project_id,
                    estimated_tokens=ESTIMATED_TOKENS[ProposalKind.DISCOVERED_FOLLOWUP],
                    case_id=ambient.case_id,
                )
            )
            if len(proposals) >= MAX_PROPOSALS_PER_RULE:
                break
        return proposals

    def _maintenance_candidates(
        self, context: Mapping[str, Any], ambient: _Ambient
    ) -> list[Proposal]:
        proposals: list[Proposal] = []
        for entry in _sequence(context.get("maintenance")):
            debt = _mapping(entry)
            if not debt:
                continue
            debt_id = _text(debt.get("id"))
            subject = _text(debt.get("title")) or _text(debt.get("summary"))
            detail = _text(debt.get("detail"))
            paths = tuple(
                sorted({_text(path) for path in _sequence(debt.get("paths")) if _text(path)})
            )
            rationale = (
                f"Declared maintenance debt {debt_id or '<unnamed>'}"
                + (f" ({', '.join(paths)})" if paths else "")
                + (f": {detail}" if detail else "")
                + ". It is not urgent, so it is proposed with a low prior and no deadline."
            )
            request = f"Pay down maintenance debt {debt_id}".strip()
            if subject:
                request = f"{request}: {subject}"
            proposals.append(
                Proposal(
                    kind=ProposalKind.MAINTENANCE_DEBT,
                    title=f"Pay down maintenance debt: {subject or debt_id or 'declared debt'}",
                    rationale=rationale,
                    confidence=BASE_CONFIDENCE[ProposalKind.MAINTENANCE_DEBT],
                    evidence=(f"debt:{debt_id}",) if debt_id else (),
                    proposed_request=request + ".",
                    project_id=_text(debt.get("project_id")) or ambient.project_id,
                    estimated_tokens=ESTIMATED_TOKENS[ProposalKind.MAINTENANCE_DEBT],
                    case_id=ambient.case_id,
                )
            )
            if len(proposals) >= MAX_PROPOSALS_PER_RULE:
                break
        return proposals

    # ── admission of our own output ──────────────────────────────

    def _admit(self, candidates: Sequence[Proposal]) -> tuple[list[Proposal], list[Refusal]]:
        """Refuse candidates without evidence; dedupe on ``(kind, evidence)``."""
        admitted: list[Proposal] = []
        refusals: list[Refusal] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for proposal in candidates:
            evidence = tuple(item for item in (_text(e) for e in proposal.evidence) if item)
            if not evidence:
                logger.warning(
                    "Refusing %s proposal %r: no evidence ids",
                    proposal.kind.value,
                    proposal.title,
                )
                refusals.append(
                    Refusal(
                        reason="proposal_without_evidence",
                        kind=proposal.kind.value,
                        title=proposal.title,
                    )
                )
                continue
            key = (proposal.kind.value, evidence)
            if key in seen:
                continue
            seen.add(key)
            admitted.append(
                replace(
                    proposal,
                    evidence=evidence,
                    confidence=_clamp_confidence(proposal.confidence),
                )
            )
        return admitted, refusals

    def _refine(self, proposal: Proposal, context: Mapping[str, Any]) -> Proposal:
        """Let the judge move confidence, and nothing else."""
        judge = self._judge
        if judge is None:  # pragma: no cover - callers check before calling
            return proposal
        try:
            raw = judge(proposal, context)
        except Exception as exc:  # a judge is untrusted: keep the rule's confidence
            logger.warning(
                "Proactive judge failed for %r; keeping rule confidence: %s",
                proposal.title,
                exc,
            )
            return proposal
        refined = _confidence_from(raw)
        if refined is None:
            return proposal
        return replace(proposal, confidence=refined)
