"""Approval HTTP surface for the dashboard: what is waiting, and the decision.

Invariant: **the gateway owns the permit; this router only projects and forwards.**
``uc_engine::governance::ApprovalStore`` is the single authority for whether an
approval is approved, spent or expired. This module reads it (``approval.list``)
and forwards a human decision (``approval.decide``) — it never decides anything
itself, and it never caches an approval.

Endpoints (mount with no prefix; the paths already carry ``/api``):

* ``GET  /api/approvals``                  — approvals, newest first
* ``POST /api/approvals/{approval_id}/decide`` — approve or reject (token-gated)

There is deliberately **no create endpoint**. A request names the principal that
wants the action (an agent, a worker), and the dashboard cannot honestly claim to
be one: authority must not be borrowed from the UI. Requests arrive over gRPC /
the governance bridge from the acting principal; the human answers here.

The decide handler forwards ``principal_kind="user"`` always. The gateway applies
two further gates that this surface must not weaken: the caller needs the
operator permission (``proactive_execute``), and only a ``user`` principal can
decide. A ``403`` from the gateway is reported as ``403`` here, not smoothed
over.

Honest limitations:

* ``GET`` is not token-gated (it exposes the decision queue, not authority);
  ``POST`` requires ``UC_DASHBOARD_TOKEN`` when that token is configured, the
  same rule the gateway's interceptor applies.
* The requester's identity, action digest and reason are shown verbatim; the
  dashboard does not interpret an action digest and must not be read as having
  verified the action it describes.
"""

from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["approvals"])

#: Governance op names this surface speaks.
OP_APPROVAL_LIST = "approval.list"
OP_APPROVAL_DECIDE = "approval.decide"

#: Environment variable carrying the gateway's shared bearer token.
TOKEN_ENV = "UC_DASHBOARD_TOKEN"
BEARER_PREFIX = "Bearer "

#: Operator identity the dashboard acts as when a human decides.
OPERATOR_ENV = "UC_OPERATOR_ID"
DEFAULT_OPERATOR = "operator"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: Page size the export asks for. It is the transport's cap (`MAX_LIST_LIMIT` in
#: Rust, 500) rounded down to a round number: bigger pages mean fewer round trips,
#: and an audit should not be one page.
EXPORT_PAGE_SIZE = 200

#: Hard bound on an export. 50 x 200 = 10 000 approvals is already far more than a
#: human queue; past it the response says it was truncated instead of pretending.
EXPORT_MAX_PAGES = 50

#: CSV column order. Raw wire fields, not the normalised view: an export is an
#: audit artifact, so it keeps the gateway's own strings (RFC 3339 timestamps)
#: instead of the epoch millis the panel renders.
EXPORT_COLUMNS = (
    "id",
    "tool",
    "case_id",
    "project_id",
    "action_digest",
    "requested_by_kind",
    "requested_by_id",
    "reason",
    "state",
    "requested_at",
    "expires_at",
    "decided_at",
    "decided_by_id",
    "note",
    "used_at",
)

#: Cells a spreadsheet would execute as a formula. A reason/note is operator-
#: supplied text, so an export must not become a vector (CSV injection).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class _GatewayUnavailableError(RuntimeError):
    """The governance bridge could not be reached (reported as ``503``)."""


def _check_token(request: Request) -> Optional[JSONResponse]:  # noqa: UP045 - py3.9
    """Enforce ``UC_DASHBOARD_TOKEN`` on mutating endpoints when configured.

    Unset means "no token required", matching the Rust gateway: an operator who
    deployed without a token already accepted an open surface, and silently
    inventing one here would break their dashboard.
    """
    expected = os.environ.get(TOKEN_ENV, "").strip()
    if not expected:
        return None
    provided = request.headers.get("authorization", "")
    if provided.startswith(BEARER_PREFIX):
        provided = provided[len(BEARER_PREFIX) :]
    if hmac.compare_digest(provided.strip(), expected):
        return None
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def _client(request: Request) -> Any:
    client = getattr(request.app.state, "governance_client", None)
    if client is None:
        raise _GatewayUnavailableError("app.state.governance_client is not configured")
    return client


async def _call_governance(
    request: Request, op: str, payload: dict[str, Any]
) -> Mapping[str, Any]:
    client = _client(request)
    try:
        reply = await client.call(op, payload)
    except Exception as exc:  # noqa: BLE001 - transport failures are all "unavailable"
        logger.warning("governance op %s failed at the transport layer: %s", op, exc)
        raise _GatewayUnavailableError(op) from exc
    if not isinstance(reply, Mapping):
        raise _GatewayUnavailableError(f"{op} returned a non-object reply")
    return reply


def _unavailable(op: str) -> JSONResponse:
    return JSONResponse(
        {"error": "governance_unavailable", "op": op}, status_code=503
    )


def _limit(value: int) -> int:
    if value <= 0:
        return DEFAULT_LIMIT
    return min(int(value), MAX_LIMIT)


@router.get("/api/approvals")
async def list_approvals(
    request: Request,
    state: Optional[str] = None,  # noqa: UP045 - FastAPI evaluates the annotation
    limit: int = DEFAULT_LIMIT,
) -> Any:
    """Return approvals, newest first. ``state`` filters pending/approved/…."""
    payload: dict[str, Any] = {"limit": _limit(limit)}
    if state:
        normalized = state.strip().lower()
        if normalized not in {"pending", "approved", "rejected", "expired", "used"}:
            return JSONResponse({"error": "invalid_state"}, status_code=400)
        payload["state"] = normalized
    try:
        reply = await _call_governance(request, OP_APPROVAL_LIST, payload)
    except _GatewayUnavailableError:
        return _unavailable(OP_APPROVAL_LIST)
    approvals = reply.get("approvals")
    if not isinstance(approvals, list):
        return JSONResponse({"error": "invalid_gateway_reply"}, status_code=502)
    return {"approvals": approvals}


@router.post("/api/approvals/{approval_id}/decide")
async def decide_approval(approval_id: str, request: Request) -> Any:
    """Approve or reject one request.

    The decision is forwarded with a ``user`` principal: the dashboard cannot act
    as a service or an agent, and doing so would defeat the point of the approval.
    """
    denied = _check_token(request)
    if denied is not None:
        return denied
    if not approval_id.strip():
        return JSONResponse({"error": "invalid_approval_id"}, status_code=400)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_body"}, status_code=400)
    if not isinstance(body, Mapping):
        return JSONResponse({"error": "invalid_body"}, status_code=400)
    approve = body.get("approve")
    if not isinstance(approve, bool):
        return JSONResponse(
            {"error": "invalid_body", "detail": "approve must be a boolean"},
            status_code=400,
        )
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        return JSONResponse(
            {"error": "invalid_body", "detail": "note must be a string"}, status_code=400
        )

    payload: dict[str, Any] = {
        "approval_id": approval_id.strip(),
        "approve": approve,
        "principal_kind": "user",
        "principal_id": os.environ.get(OPERATOR_ENV, DEFAULT_OPERATOR).strip()
        or DEFAULT_OPERATOR,
    }
    if note is not None:
        payload["note"] = note
    try:
        reply = await _call_governance(request, OP_APPROVAL_DECIDE, payload)
    except _GatewayUnavailableError:
        return _unavailable(OP_APPROVAL_DECIDE)
    approval = reply.get("approval")
    if not isinstance(approval, Mapping):
        return JSONResponse({"error": "invalid_gateway_reply"}, status_code=502)
    return {"approval": approval}


def _csv_cell(value: Any) -> str:
    """Render one wire value for a spreadsheet without ever becoming a formula."""
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if text.startswith(_FORMULA_PREFIXES):
        # Prefix with a quote so a spreadsheet shows the text instead of running it.
        return "'" + text
    return text


@router.get("/api/approvals/export.csv")
async def export_approvals(
    request: Request,
    state: Optional[str] = None,  # noqa: UP045 - FastAPI evaluates the annotation
) -> Any:
    """Export the approval history as CSV, walking the gateway's pages.

    The list op is capped by the transport (500 per call), and an audit is not one
    page. This endpoint therefore pages by `offset` until a short page arrives,
    bounded by :data:`EXPORT_MAX_PAGES`; when the bound is hit the response says so
    in ``X-UC-Approvals-Truncated`` instead of silently returning a shorter file.

    Read-only and not token-gated, like the rest of the approval reads: it exposes
    governance metadata, not authority. Cells that a spreadsheet would execute as a
    formula are prefixed with a quote.
    """
    payload: dict[str, Any] = {}
    if state:
        normalized = state.strip().lower()
        if normalized not in {"pending", "approved", "rejected", "expired", "used"}:
            return JSONResponse({"error": "invalid_state"}, status_code=400)
        payload["state"] = normalized

    rows: list[Mapping[str, Any]] = []
    truncated = False
    offset = 0
    for _page in range(EXPORT_MAX_PAGES):
        try:
            reply = await _call_governance(
                request,
                OP_APPROVAL_LIST,
                {**payload, "limit": EXPORT_PAGE_SIZE, "offset": offset},
            )
        except _GatewayUnavailableError:
            return _unavailable(OP_APPROVAL_LIST)
        page = reply.get("approvals")
        if not isinstance(page, list):
            return JSONResponse({"error": "invalid_gateway_reply"}, status_code=502)
        rows.extend(entry for entry in page if isinstance(entry, Mapping))
        if len(page) < EXPORT_PAGE_SIZE:
            break
        offset += EXPORT_PAGE_SIZE
    else:
        truncated = True

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in EXPORT_COLUMNS])

    headers = {
        "Content-Disposition": 'attachment; filename="approvals.csv"',
        "X-UC-Approvals-Returned": str(len(rows)),
        "Cache-Control": "no-store",
    }
    if truncated:
        headers["X-UC-Approvals-Truncated"] = "true"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


__all__ = [
    "router",
    "OP_APPROVAL_LIST",
    "OP_APPROVAL_DECIDE",
    "EXPORT_COLUMNS",
    "EXPORT_MAX_PAGES",
    "EXPORT_PAGE_SIZE",
]
