"""Attention, capacity and case-worklist HTTP surface for the dashboard.

Invariant: **the gateway owns governance state; this router only projects it.**
Every handler reads the NATS request/reply client from
``app.state.governance_client`` and calls exactly one governance op. There is no
dashboard-side attention state: a resolve is forwarded, and the gateway's
idempotent answer is returned unchanged, so a double click cannot resolve an
item twice. When the client is missing, or reports the gateway unavailable, the
answer is ``503 {"error": "governance_unavailable"}`` — never a 500 traceback,
because a dead bridge is an operational state, not a bug.

Endpoints (mount with no prefix; the paths already carry ``/api``):

* ``GET /api/attention`` — worklist items in canonical order, plus capacity.
* ``POST /api/attention/{attention_id}/resolve`` — record a human decision.
* ``GET /api/capacity`` — the capacity snapshot.
* ``GET /api/cases`` / ``GET /api/cases/{case_id}`` — read-only case views.

The worklist order is re-applied here rather than trusted from the gateway, so
two clients asking at the same time always see the same worklist: human
decisions first, then priority descending, then the oldest ``since``, then
``case_id``.

Honest limitations:

* Read endpoints are not token-gated here. They expose governance metadata, not
  authority, and the embedding app has its own UI auth (``DASHBOARD_PASSWORD``).
  The one mutating endpoint requires ``UC_DASHBOARD_TOKEN`` when that token is
  configured and is open when the deployment left it unset — the same rule the
  Rust gateway's auth interceptor applies.
* ``attention.resolve`` always acts as a ``user`` principal. The dashboard has
  no way to act as a service or an agent, by design: authority must not be
  borrowed from the UI.
* Attention items are re-projected through :class:`AttentionItem`, so the wire
  shape is exactly the contract and the order is canonical; a gateway field
  outside that contract is dropped here. Cases are forwarded verbatim, because
  the gateway owns their (larger) schema.
* A malformed gateway reply (no ``item`` / no ``case``) is reported as ``404``
  when the object is simply absent and as ``502`` when it is present but
  unusable; the governance contract does not distinguish the two, so the
  dashboard picks the honest HTTP meaning for each.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from collections.abc import Mapping
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ultimate_coders.attention import (
    AttentionItem,
    AttentionKind,
    Capacity,
    CapacityTracker,
    as_slot_count,
    worklist_order,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["attention"])

#: Governance op names this surface speaks.
OP_ATTENTION_LIST = "attention.list"
OP_ATTENTION_RESOLVE = "attention.resolve"
OP_CAPACITY_GET = "capacity.get"
OP_CASE_LIST = "case.list"
OP_CASE_GET = "case.get"

#: Environment variable carrying the gateway's shared bearer token.
TOKEN_ENV = "UC_DASHBOARD_TOKEN"
BEARER_PREFIX = "Bearer "

#: Default and maximum page size for the worklist / case list.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

#: Resolution kinds the gateway accepts for ``attention.resolve``.
RESOLUTION_KINDS = ("human_decision", "event_received", "automatic", "case_closed")

#: Case status tokens the gateway accepts for ``case.list`` (Rust ``CaseStatus``).
CASE_STATUSES = (
    "open",
    "active",
    "waiting_event",
    "needs_human",
    "blocked",
    "verifying",
    "completed",
    "failed",
    "cancelled",
)

#: Transport-level failures that mean "the bridge is down", not "the request is bad".
_BRIDGE_FAILURES: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
    asyncio.TimeoutError,
)


class _GatewayUnavailableError(Exception):
    """Internal: the governance client is missing or the gateway is unavailable."""


def _unavailable_exception_types() -> tuple[type[BaseException], ...]:
    """Return the gateway's own unavailable exception type, when importable.

    ``GovernanceUnavailable`` lives in ``ultimate_coders.governance_client``,
    which is wired by the deployment rather than by this router. The import is
    lazy so this module stays importable (and testable with a fake client)
    without it; an empty tuple simply means "match by class name only".
    """
    try:
        from ultimate_coders.governance_client import GovernanceUnavailable
    except Exception:  # pragma: no cover - the module is optional at import time
        return ()
    return (GovernanceUnavailable,)


def _is_unavailable_error(exc: BaseException) -> bool:
    """True when ``exc`` reports the gateway as unavailable rather than a bug.

    The class-name check exists because a test fake may raise its own
    ``GovernanceUnavailable``; both spellings mean the same thing to a caller.
    """
    if type(exc).__name__ == "GovernanceUnavailable":
        return True
    types = _unavailable_exception_types()
    return bool(types) and isinstance(exc, types)


def _unavailable() -> JSONResponse:
    """The single 503 shape every handler returns for a dead bridge."""
    return JSONResponse({"error": "governance_unavailable"}, status_code=503)


def _check_token(request: Request) -> JSONResponse | None:
    """Gate a mutation on ``UC_DASHBOARD_TOKEN`` when the deployment sets one.

    No token configured means open access (backwards compatible with local
    deployments). No localhost bypass: unlike ``DASHBOARD_PASSWORD``, this is
    the gateway's shared bearer token, and a mutation either presents it or the
    deployment left the gate open on purpose.
    """
    expected = (os.environ.get(TOKEN_ENV) or "").strip()
    if not expected:
        return None
    header = request.headers.get("authorization", "")
    provided = header[len(BEARER_PREFIX) :].strip() if header.startswith(BEARER_PREFIX) else ""
    if provided and hmac.compare_digest(provided, expected):
        return None
    return JSONResponse({"error": "unauthorized"}, status_code=401)


async def _call_governance(
    request: Request, op: str, payload: dict[str, Any]
) -> Mapping[str, Any]:
    """Call one governance op, mapping a dead bridge to ``_GatewayUnavailableError``.

    Anything that is not an availability problem propagates: a bug in a handler
    must be visible as a 500, not disguised as an outage.
    """
    client = getattr(request.app.state, "governance_client", None)
    if client is None:
        raise _GatewayUnavailableError("app.state.governance_client is not configured")
    try:
        reply = await client.call(op, payload)
    except _BRIDGE_FAILURES as exc:
        logger.warning("governance op %s failed at the transport layer: %s", op, exc)
        raise _GatewayUnavailableError(op) from exc
    except Exception as exc:
        if not _is_unavailable_error(exc):
            raise
        logger.warning("governance op %s unavailable: %s", op, exc)
        raise _GatewayUnavailableError(op) from exc
    if not isinstance(reply, Mapping):
        logger.warning(
            "governance op %s returned %s, not a mapping", op, type(reply).__name__
        )
        raise _GatewayUnavailableError(op)
    return reply


def _limit(value: int) -> int:
    """Clamp a page size into ``[0, MAX_LIMIT]`` (the repo's limit convention)."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(0, min(limit, MAX_LIMIT))


def _project_items(raw: Any) -> list[AttentionItem]:
    """Project wire items and return them in canonical worklist order.

    An item with an unusable identity or an unknown kind is skipped with a
    warning rather than failing the whole worklist: one future-gateway record
    must not blind the operator to every other pending decision.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    items: list[AttentionItem] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            logger.warning("skipping attention item that is not an object")
            continue
        try:
            items.append(AttentionItem.from_dict(entry))
        except ValueError as exc:
            logger.warning("skipping unprojectable attention item: %s", exc)
    return worklist_order(items)


def _capacity_from_reply(reply: Mapping[str, Any], items: list[AttentionItem]) -> Capacity:
    """Return the reply's capacity, or derive the item-side counters locally.

    An older gateway that omits ``capacity`` still gets a usable answer: the
    counters that come from items are computed here, and the slot counters are
    reported as zero because the dashboard does not own the worker registry.
    """
    raw = reply.get("capacity")
    if isinstance(raw, Mapping):
        return Capacity.from_dict(raw)
    logger.warning("attention.list reply carried no capacity; deriving it from items")
    return CapacityTracker(human_reserve=0).project(
        items, worker_slots_total=0, worker_slots_busy=0
    )


def _case_status(value: str) -> str | None:
    """Normalise a case-status filter, or return ``None`` when it is unknown."""
    token = value.strip().lower()
    if token == "canceled":
        token = "cancelled"
    return token if token in CASE_STATUSES else None


@router.get("/api/attention")
async def list_attention(
    request: Request,
    include_resolved: bool = False,
    kind: Optional[str] = None,  # noqa: UP045 - FastAPI evaluates the annotation
    case_id: Optional[str] = None,  # noqa: UP045
    limit: int = DEFAULT_LIMIT,
) -> Any:
    """Return the operator worklist in canonical order, plus capacity."""
    selected = None
    if kind is not None:
        selected = AttentionKind.from_str_lossy(kind)
        if selected is None:
            return JSONResponse({"error": "invalid_kind"}, status_code=400)
    payload: dict[str, Any] = {
        "include_resolved": bool(include_resolved),
        "limit": _limit(limit),
    }
    if selected is not None:
        payload["kind"] = selected.value
    if case_id:
        payload["case_id"] = case_id
    try:
        reply = await _call_governance(request, OP_ATTENTION_LIST, payload)
    except _GatewayUnavailableError:
        return _unavailable()
    items = _project_items(reply.get("items"))
    return {
        "items": [item.to_dict() for item in items],
        "capacity": _capacity_from_reply(reply, items).to_dict(),
    }


@router.post("/api/attention/{attention_id}/resolve")
async def resolve_attention(attention_id: str, request: Request) -> Any:
    """Record the resolution of one attention item.

    Idempotent by contract: resolving an already-resolved item returns the
    original resolution, so a double submit is harmless. The body is parsed by
    hand instead of through a Pydantic model so a malformed body gets a precise
    ``400`` rather than a schema dump.
    """
    denied = _check_token(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_body"}, status_code=400)
    if not isinstance(body, Mapping):
        return JSONResponse({"error": "invalid_body"}, status_code=400)
    resolution_kind = body.get("resolution_kind")
    normalized = resolution_kind.strip().lower() if isinstance(resolution_kind, str) else ""
    if normalized not in RESOLUTION_KINDS:
        return JSONResponse({"error": "invalid_resolution_kind"}, status_code=400)
    by = body.get("by")
    detail = body.get("detail")
    if by is not None and not isinstance(by, str):
        return JSONResponse({"error": "invalid_by"}, status_code=400)
    if detail is not None and not isinstance(detail, str):
        return JSONResponse({"error": "invalid_detail"}, status_code=400)
    resolver = (by or "").strip() or "dashboard"
    payload: dict[str, Any] = {
        "attention_id": attention_id,
        "resolution_kind": normalized,
        # The UI never borrows a service or agent identity (see module docstring).
        "principal_kind": "user",
        "principal_id": resolver,
    }
    if by:
        payload["by"] = by
    if detail:
        payload["detail"] = detail
    try:
        reply = await _call_governance(request, OP_ATTENTION_RESOLVE, payload)
    except _GatewayUnavailableError:
        return _unavailable()
    raw = reply.get("item")
    if not isinstance(raw, Mapping):
        return JSONResponse({"error": "attention_not_found"}, status_code=404)
    try:
        item = AttentionItem.from_dict(raw)
    except ValueError as exc:
        logger.warning("attention.resolve returned an unprojectable item: %s", exc)
        return JSONResponse({"error": "invalid_gateway_reply"}, status_code=502)
    return {"item": item.to_dict()}


@router.get("/api/capacity")
async def get_capacity(request: Request) -> Any:
    """Return the capacity snapshot the scheduler reports."""
    try:
        reply = await _call_governance(request, OP_CAPACITY_GET, {})
    except _GatewayUnavailableError:
        return _unavailable()
    raw = reply.get("capacity")
    capacity = Capacity.from_dict(raw if isinstance(raw, Mapping) else None)
    return {"capacity": capacity.to_dict()}


@router.get("/api/cases")
async def list_cases(
    request: Request,
    status: Optional[str] = None,  # noqa: UP045 - FastAPI evaluates the annotation
    project_id: Optional[str] = None,  # noqa: UP045
    thread_key: Optional[str] = None,  # noqa: UP045
    include_terminal: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> Any:
    """Return governance cases for the worklist (read-only projection)."""
    selected = None
    if status is not None:
        selected = _case_status(status)
        if selected is None:
            return JSONResponse({"error": "invalid_status"}, status_code=400)
    payload: dict[str, Any] = {
        "include_terminal": bool(include_terminal),
        "limit": _limit(limit),
    }
    if selected is not None:
        payload["status"] = selected
    if project_id:
        payload["project_id"] = project_id
    if thread_key:
        payload["thread_key"] = thread_key
    try:
        reply = await _call_governance(request, OP_CASE_LIST, payload)
    except _GatewayUnavailableError:
        return _unavailable()
    raw = reply.get("cases")
    cases = (
        [dict(case) for case in raw if isinstance(case, Mapping)]
        if isinstance(raw, (list, tuple))
        else []
    )
    total = as_slot_count(reply.get("total"))
    if total < len(cases):
        # A partial or older reply must not under-report: the list is the evidence.
        total = len(cases)
    return {"cases": cases, "total": total}


@router.get("/api/cases/{case_id}")
async def get_case(case_id: str, request: Request) -> Any:
    """Return one governance case (read-only projection)."""
    try:
        reply = await _call_governance(request, OP_CASE_GET, {"case_id": case_id})
    except _GatewayUnavailableError:
        return _unavailable()
    raw = reply.get("case")
    if not isinstance(raw, Mapping):
        return JSONResponse({"error": "case_not_found"}, status_code=404)
    return {"case": dict(raw)}


__all__ = ["router"]
