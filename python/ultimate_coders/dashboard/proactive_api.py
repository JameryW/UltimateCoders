"""Proactive governance HTTP surface: modes, proposals, audit and watchdog input.

Invariant: this module is a **transport, not a policy engine**. Every admission,
budget, state and execution-authorization decision is made by the Rust gateway
over the governance bridge; the handlers here validate the request *shape*,
forward it as a governance op, and surface the gateway's answer -- including its
deny code -- to the caller. Nothing here retries, executes, caches authority or
re-derives a policy answer.

Consequences that are deliberate:

* an empty ``evidence`` list is forwarded, not rejected locally. The gateway
  refuses it (``deny_code`` + ``deny_detail``) and records the refusal in its
  audit trail; duplicating that rule here would let the two drift, and this layer
  must not be able to disagree with the control plane.
* a denied proposal still answers ``200`` with ``accepted: false``. The request
  succeeded; the answer was no. The deny reason is logged here as well, so a
  false trigger is auditable from the log and from ``GET /api/proactive/audit``.
* a missing governance client, or a bridge that cannot answer, is ``503``
  ``{"error": "governance_unavailable"}`` -- never a ``500`` traceback and never
  a fabricated empty state (an empty proposal list would read as "nothing to
  review").
* a gateway *rejection* (``GovernanceError``: policy denial, illegal transition,
  conflict) is a ``400`` carrying the gateway's own ``code``. The transport does
  not re-classify denials into 403/409: that mapping would be a second policy
  that can drift from the gateway's.

Auth: the host application owns authentication. :func:`build_router` takes the
dashboard's gate as an argument instead of growing a second scheme here, and the
module-level :data:`router` applies the host's check when the host advertises it
on ``app.state.dashboard_auth_check`` (``DashboardApp._check_auth`` has the right
signature). A host that mounts the plain router without that attribute exposes
these endpoints ungated -- the existing dashboard gate is the intended wiring.

Honest limitations:

* Request validation is shape-only (types, tokens, ranges). Semantic refusals
  belong to the gateway, so a shape-valid request can still be denied.
* No caching: every call hits the bridge. A dashboard that polls the state
  endpoint adds bridge load; there is no local mirror to go stale.
* The principal defaults to ``user``/``dashboard`` when the request omits it.
  That is an attribution default, not an authorization: the gateway still
  authorizes whatever principal it receives.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ultimate_coders.governance_client import GovernanceError, GovernanceUnavailable
from ultimate_coders.proactive.evaluator import PROPOSAL_KIND_TOKENS
from ultimate_coders.proactive.modes import parse_proactive_mode
from ultimate_coders.proactive.watchdog_input import normalize_stall_report

logger = logging.getLogger(__name__)

#: Default principal for dashboard-originated actions. Overridable per request.
DEFAULT_PRINCIPAL_KIND = "user"
DEFAULT_PRINCIPAL_ID = "dashboard"

#: Proposal lifecycle tokens accepted as a list filter (gateway ``ProposalStatus``).
PROPOSAL_STATUSES: tuple[str, ...] = (
    "observed",
    "proposed",
    "approved",
    "rejected",
    "executed",
    "expired",
    "failed",
)

#: List endpoints default to and clamp to this many rows.
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500

#: The host's authentication gate: a response when denied, ``None`` when allowed.
AuthCheck = Callable[[Request], Optional[Response]]


class _RequestError(ValueError):
    """A request this layer can reject on shape alone."""

    def __init__(self, error: str, detail: str = "") -> None:
        super().__init__(detail or error)
        self.error = error
        self.detail = detail

    def response(self) -> JSONResponse:
        return _bad_request(self.error, detail=self.detail)


def _bad_request(error: str, *, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=400)


def _unavailable(op: str, exc: BaseException) -> JSONResponse:
    """Map a bridge failure to 503, keeping the cause on the record."""
    logger.warning("Governance bridge unavailable for %s: %s", op, exc)
    return JSONResponse({"error": "governance_unavailable"}, status_code=503)


def _rejected(op: str, exc: GovernanceError) -> JSONResponse:
    """Map a gateway rejection to 4xx, carrying the gateway's own code.

    The status is generic on purpose: the machine-readable reason is ``code``
    (``proactive_mode_disallows``, ``budget_exhausted``, ``missing_permission``,
    ...), and re-classifying denials into 403/409 here would be a second policy
    that can drift from the gateway's.
    """
    logger.info("Governance rejected %s: %s", op, exc)
    return JSONResponse({"error": exc.code, "detail": exc.message}, status_code=400)


async def _call_governance(request: Request, op: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Forward one governance op, or raise :class:`GovernanceUnavailable`.

    A response that is not an object is treated as unavailable rather than
    passed through: the caller asked a question and did not get an answer it can
    reason about.
    """
    client = getattr(request.app.state, "governance_client", None)
    if client is None:
        raise GovernanceUnavailable(f"no governance client is configured for {op}")
    result = await client.call(op, payload)
    if not isinstance(result, dict):
        raise GovernanceUnavailable(f"{op} returned {type(result).__name__}, expected an object")
    return result


async def _read_object(request: Request) -> dict[str, Any]:
    """Parse a JSON object body, rejecting anything else on shape."""
    try:
        body = await request.json()
    except Exception as exc:  # any parse failure is a 400, never a 500
        raise _RequestError("invalid_json", "request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise _RequestError("invalid_body", "request body must be a JSON object")
    return body


# ── payload builders (pure; used by the handlers and asserted by tests) ──


def _principal(body: Mapping[str, Any]) -> tuple[str, str]:
    kind = body.get("principal_kind", DEFAULT_PRINCIPAL_KIND)
    ident = body.get("principal_id", DEFAULT_PRINCIPAL_ID)
    if not isinstance(kind, str) or not kind.strip():
        raise _RequestError("invalid_principal", "principal_kind must be a non-empty string")
    if not isinstance(ident, str) or not ident.strip():
        raise _RequestError("invalid_principal", "principal_id must be a non-empty string")
    return kind.strip(), ident.strip()


def _optional_str(body: Mapping[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _RequestError(f"invalid_{name}", f"{name} must be a string")
    stripped = value.strip()
    return stripped or None


def _optional_int(body: Mapping[str, Any], name: str, *, minimum: int = 0) -> int | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _RequestError(f"invalid_{name}", f"{name} must be an integer")
    if value < minimum:
        raise _RequestError(f"invalid_{name}", f"{name} must be >= {minimum}")
    return value


def _optional_bool(body: Mapping[str, Any], name: str) -> bool | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _RequestError(f"invalid_{name}", f"{name} must be a boolean")
    return value


def mode_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    """``proactive.mode.set`` payload. An unknown mode token is a 400.

    A typo must not be coerced to a valid mode: the operator asked for something
    that would not happen, and saying so is the only honest answer. A blank mode
    is rejected too -- the environment variable's empty string means "unset, use
    the default", but an explicit API call has no such reading.
    """
    raw_mode = body.get("mode")
    if not isinstance(raw_mode, str) or not raw_mode.strip():
        raise _RequestError(
            "invalid_mode",
            f"mode must be one of off/observe/propose/execute; got {raw_mode!r}",
        )
    mode = parse_proactive_mode(raw_mode)
    if mode is None:
        raise _RequestError(
            "invalid_mode",
            f"mode must be one of off/observe/propose/execute; got {raw_mode!r}",
        )
    payload: dict[str, Any] = {"mode": mode.value}
    max_open = _optional_int(body, "max_open_proposals")
    if max_open is not None:
        payload["max_open_proposals"] = max_open
    max_tokens = _optional_int(body, "max_tokens_per_action")
    if max_tokens is not None:
        payload["max_tokens_per_action"] = max_tokens
    always_approve = _optional_bool(body, "always_require_approval")
    if always_approve is not None:
        payload["always_require_approval"] = always_approve
    reason = _optional_str(body, "reason")
    if reason:
        payload["reason"] = reason
    kind, ident = _principal(body)
    payload["principal_kind"] = kind
    payload["principal_id"] = ident
    return payload


def proposal_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    """``proactive.proposal.submit`` payload (shape only; evidence policy is the gateway's)."""
    kind = body.get("kind")
    if kind not in PROPOSAL_KIND_TOKENS:
        raise _RequestError(
            "invalid_kind",
            f"kind must be one of {', '.join(PROPOSAL_KIND_TOKENS)}; got {kind!r}",
        )
    title = _optional_str(body, "title")
    rationale = _optional_str(body, "rationale")
    if not title:
        raise _RequestError("invalid_title", "title must be a non-empty string")
    if not rationale:
        raise _RequestError("invalid_rationale", "rationale must be a non-empty string")
    confidence = body.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise _RequestError("invalid_confidence", "confidence must be a number in [0, 1]")
    if not 0.0 <= float(confidence) <= 1.0:
        raise _RequestError("invalid_confidence", "confidence must be a number in [0, 1]")
    evidence = body.get("evidence")
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise _RequestError("invalid_evidence", "evidence must be a list of strings")
    cleaned_evidence = [item.strip() for item in evidence]
    if any(not item for item in cleaned_evidence):
        raise _RequestError("invalid_evidence", "evidence ids must be non-empty strings")
    payload: dict[str, Any] = {
        "kind": kind,
        "title": title,
        "rationale": rationale,
        "confidence": float(confidence),
        "evidence": cleaned_evidence,
    }
    for name in ("case_id", "proposed_request", "project_id"):
        value = _optional_str(body, name)
        if value:
            payload[name] = value
    estimated = _optional_int(body, "estimated_tokens")
    if estimated is not None:
        payload["estimated_tokens"] = estimated
    principal_kind, principal_id = _principal(body)
    payload["principal_kind"] = principal_kind
    payload["principal_id"] = principal_id
    return payload


def decision_payload(proposal_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """``proactive.proposal.decide`` payload."""
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        raise _RequestError("invalid_decision", 'decision must be "approve" or "reject"')
    payload: dict[str, Any] = {
        "proposal_id": proposal_id,
        "decision": decision,
        "by": _optional_str(body, "by") or DEFAULT_PRINCIPAL_ID,
    }
    reason = _optional_str(body, "reason")
    if reason:
        payload["reason"] = reason
    principal_kind, principal_id = _principal(body)
    payload["principal_kind"] = principal_kind
    payload["principal_id"] = principal_id
    return payload


def _limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    if raw is None or not raw.strip():
        return DEFAULT_LIST_LIMIT
    try:
        value = int(raw)
    except ValueError as exc:
        raise _RequestError("invalid_limit", "limit must be an integer") from exc
    if not 1 <= value <= MAX_LIST_LIMIT:
        raise _RequestError("invalid_limit", f"limit must be between 1 and {MAX_LIST_LIMIT}")
    return value


def proposals_query(request: Request) -> dict[str, Any]:
    """``proactive.proposal.list`` payload from the query string."""
    payload: dict[str, Any] = {"limit": _limit(request)}
    status = (request.query_params.get("status") or "").strip()
    if status:
        if status not in PROPOSAL_STATUSES:
            raise _RequestError(
                "invalid_status",
                f"status must be one of {', '.join(PROPOSAL_STATUSES)}; got {status!r}",
            )
        payload["status"] = status
    case_id = (request.query_params.get("case_id") or "").strip()
    if case_id:
        payload["case_id"] = case_id
    return payload


def audit_query(request: Request) -> dict[str, Any]:
    """``proactive.audit.list`` payload from the query string."""
    payload: dict[str, Any] = {"limit": _limit(request)}
    proposal_id = (request.query_params.get("proposal_id") or "").strip()
    if proposal_id:
        payload["proposal_id"] = proposal_id
    case_id = (request.query_params.get("case_id") or "").strip()
    if case_id:
        payload["case_id"] = case_id
    return payload


def stall_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    """``watchdog.stall.report`` payload, validated by the watchdog input module."""
    try:
        return normalize_stall_report(body)
    except ValueError as exc:
        raise _RequestError("invalid_stall_report", str(exc)) from exc


# ── router ────────────────────────────────────────────────────

#: App-state attribute a host sets to gate these endpoints with its own
#: authentication. ``DashboardApp._check_auth`` has the right signature.
AUTH_CHECK_STATE_ATTR = "dashboard_auth_check"


def _state_auth_check(request: Request) -> Response | None:
    """Apply the host's gate when it advertised one on ``app.state``.

    The dashboard composes this router through
    ``ultimate_coders.dashboard.governance_routes``, which only knows the
    module-level :data:`router`; advertising the check on ``app.state`` keeps one
    authentication scheme (the host's) instead of adding a second one here.
    """
    checker = getattr(request.app.state, AUTH_CHECK_STATE_ATTR, None)
    if checker is None:
        return None
    return checker(request)


def build_router(*, auth: AuthCheck | None = None) -> APIRouter:
    """Build the proactive router, optionally behind the host's auth gate.

    ``auth`` is the host application's own check (``DashboardApp._check_auth``
    returns a response when denied and ``None`` when allowed). Injecting it keeps
    one authentication scheme in the system. The module-level :data:`router` uses
    :func:`_state_auth_check`, which applies the host's check when the host sets
    ``app.state.dashboard_auth_check``; without it the endpoints are ungated.
    """
    router = APIRouter(tags=["proactive"])

    def _denied(request: Request) -> Response | None:
        return auth(request) if auth is not None else None

    @router.get("/api/proactive/state")
    async def proactive_state(request: Request):
        """The gateway's proactive gate state (mode, caps, counters)."""
        if (denied := _denied(request)) is not None:
            return denied
        try:
            result = await _call_governance(request, "proactive.state.get", {})
        except GovernanceUnavailable as exc:
            return _unavailable("proactive.state.get", exc)
        except GovernanceError as exc:
            return _rejected("proactive.state.get", exc)
        return JSONResponse(result)

    @router.post("/api/proactive/mode")
    async def proactive_set_mode(request: Request):
        """Change the autonomy ceiling. The gateway authorizes the principal."""
        if (denied := _denied(request)) is not None:
            return denied
        try:
            payload = mode_payload(await _read_object(request))
        except _RequestError as exc:
            return exc.response()
        try:
            result = await _call_governance(request, "proactive.mode.set", payload)
        except GovernanceUnavailable as exc:
            return _unavailable("proactive.mode.set", exc)
        except GovernanceError as exc:
            return _rejected("proactive.mode.set", exc)
        return JSONResponse(result)

    @router.get("/api/proactive/proposals")
    async def proactive_list_proposals(request: Request):
        """List proposals, optionally filtered by status or case."""
        if (denied := _denied(request)) is not None:
            return denied
        try:
            payload = proposals_query(request)
        except _RequestError as exc:
            return exc.response()
        try:
            result = await _call_governance(request, "proactive.proposal.list", payload)
        except GovernanceUnavailable as exc:
            return _unavailable("proactive.proposal.list", exc)
        except GovernanceError as exc:
            return _rejected("proactive.proposal.list", exc)
        return JSONResponse(result)

    @router.post("/api/proactive/proposals")
    async def proactive_submit_proposal(request: Request):
        """Submit one evaluated proposal. Admission is the gateway's answer."""
        if (denied := _denied(request)) is not None:
            return denied
        try:
            payload = proposal_payload(await _read_object(request))
        except _RequestError as exc:
            return exc.response()
        try:
            result = await _call_governance(request, "proactive.proposal.submit", payload)
        except GovernanceUnavailable as exc:
            return _unavailable("proactive.proposal.submit", exc)
        except GovernanceError as exc:
            return _rejected("proactive.proposal.submit", exc)
        if result.get("accepted") is False:
            logger.warning(
                "Proactive proposal refused: kind=%s deny_code=%s deny_detail=%s evidence=%s",
                payload.get("kind"),
                result.get("deny_code"),
                result.get("deny_detail"),
                payload.get("evidence"),
            )
        return JSONResponse(result)

    @router.post("/api/proactive/proposals/{proposal_id}/decide")
    async def proactive_decide_proposal(proposal_id: str, request: Request):
        """Approve or reject one proposal (a human decision, recorded by the gateway)."""
        if (denied := _denied(request)) is not None:
            return denied
        try:
            payload = decision_payload(proposal_id, await _read_object(request))
        except _RequestError as exc:
            return exc.response()
        try:
            result = await _call_governance(request, "proactive.proposal.decide", payload)
        except GovernanceUnavailable as exc:
            return _unavailable("proactive.proposal.decide", exc)
        except GovernanceError as exc:
            return _rejected("proactive.proposal.decide", exc)
        return JSONResponse(result)

    @router.get("/api/proactive/audit")
    async def proactive_audit(request: Request):
        """The gateway's audit trail: every gate result, allowed or denied."""
        if (denied := _denied(request)) is not None:
            return denied
        try:
            payload = audit_query(request)
        except _RequestError as exc:
            return exc.response()
        try:
            result = await _call_governance(request, "proactive.audit.list", payload)
        except GovernanceUnavailable as exc:
            return _unavailable("proactive.audit.list", exc)
        except GovernanceError as exc:
            return _rejected("proactive.audit.list", exc)
        return JSONResponse(result)

    @router.post("/api/watchdog/stall")
    async def watchdog_report_stall(request: Request):
        """Report deterministic stall evidence; the gateway returns the bounded verdict."""
        if (denied := _denied(request)) is not None:
            return denied
        try:
            payload = stall_payload(await _read_object(request))
        except _RequestError as exc:
            return exc.response()
        try:
            result = await _call_governance(request, "watchdog.stall.report", payload)
        except GovernanceUnavailable as exc:
            return _unavailable("watchdog.stall.report", exc)
        except GovernanceError as exc:
            return _rejected("watchdog.stall.report", exc)
        return JSONResponse(result)

    return router


#: Composed instance. The host's auth check is applied when it sets
#: ``app.state.dashboard_auth_check``; otherwise mount :func:`build_router` with
#: ``auth=...`` instead.
router = build_router(auth=_state_auth_check)
