"""Dashboard trigger surface: the only HTTP door into the trigger inbox.

The gateway owns ingest: dedup, subscription matching and case routing all live
there, behind one canonical payload. This module owns the door: authenticate the
delivery, reduce it to the canonical shape, forward it, and report exactly what
the gateway said. It never fabricates an ingest outcome locally -- a local
"accepted" would be a lie the gateway cannot undo, so an unreachable gateway is
a 503, not a success.

Two authentication rules, deliberately different:

* ``POST /api/triggers/github`` is reachable by anyone who can reach the port,
  so an unconfigured ``UC_GITHUB_WEBHOOK_SECRET`` **fails closed** (503). A
  signature is checked over the raw body before anything is parsed.
* ``POST /api/triggers/ingest`` is an operator/automation surface and follows
  the dashboard's existing convention: when ``UC_DASHBOARD_TOKEN`` is set a
  Bearer token is required, and when it is unset auth is disabled (the outer
  dashboard deployment is expected to gate the port).

Honest limitations: the read surface (``GET /api/triggers/subscriptions``) is
read-only and inherits whatever outer gate the dashboard wiring applies; this
router only enforces the write-path token. Triggers carry no authority, so
nothing here decides what an event may cause -- that is the gateway's mandate.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ultimate_coders.trigger.github_webhook import (
    MAX_PAYLOAD_BYTES,
    ParsedEvent,
    PayloadTooLargeError,
    WebhookPayloadError,
    canonical_payload_json,
    ingest_payload,
    is_known_kind,
    is_known_source,
    parse_webhook,
    verify_signature,
)

try:  # The bridge module is owned by the gateway wiring; use its type when present.
    from ultimate_coders.governance_client import GovernanceUnavailable
except ImportError:  # pragma: no cover - exercised only before the bridge lands

    class GovernanceUnavailable(Exception):  # noqa: N818 - name fixed by the bridge contract
        """Marker for "the gateway is unreachable" when the bridge is absent.

        The API maps this marker to 503 either way; importing the real class
        when it exists keeps ``except`` clauses and tests pointed at one type.
        """


router = APIRouter()

GITHUB_WEBHOOK_SECRET_ENV = "UC_GITHUB_WEBHOOK_SECRET"
DASHBOARD_TOKEN_ENV = "UC_DASHBOARD_TOKEN"
GOVERNANCE_UNAVAILABLE_ERROR = "governance_unavailable"

#: The dashboard is the authenticated caller of the gateway; a trigger event
#: itself never holds a permission (see the package docstring).
PRINCIPAL_KIND = "service"
PRINCIPAL_ID = "dashboard"


# ── Gateway bridge ─────────────────────────────────────────────────────


#: Transport-level failures that mean "the bridge is down", not "the request is
#: wrong". The real client wraps these in ``GovernanceUnavailable``; keeping them
#: here means an older or simpler client cannot turn an outage into a 500.
_BRIDGE_FAILURES: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError)


def _unavailable_response() -> JSONResponse:
    return JSONResponse({"error": GOVERNANCE_UNAVAILABLE_ERROR}, status_code=503)


async def _call(
    request: Request, op: str, payload: dict[str, Any]
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Call the governance bridge, mapping "not there" to 503.

    Returns ``(result, None)`` on success or ``(None, response)`` when the
    caller must return early. A bridge reply that is not an object is a
    contract violation (502), not a reason to invent an outcome. Any other
    exception propagates: a handler bug must be visible as a 500, not disguised
    as an outage.
    """
    client = getattr(request.app.state, "governance_client", None)
    if client is None:
        return None, _unavailable_response()
    try:
        result = await client.call(op, payload)
    except (GovernanceUnavailable, *_BRIDGE_FAILURES):
        return None, _unavailable_response()
    if not isinstance(result, dict):
        return None, _error(502, "governance_bad_reply")
    return result, None


# ── Request helpers ────────────────────────────────────────────────────


def _error(status_code: int, error: str, detail: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status_code)


def _declared_content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None or not raw.strip().isdigit():
        return None
    return int(raw.strip())


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer ") :].strip()
    return ""


def _require_dashboard_token(request: Request) -> JSONResponse | None:
    """Enforce ``UC_DASHBOARD_TOKEN`` when an operator configured it."""
    expected = os.environ.get(DASHBOARD_TOKEN_ENV, "")
    if not expected:
        return None
    supplied = _bearer_token(request)
    # Bytes on both sides: a non-ASCII header must compare false, not raise.
    if supplied and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return None
    return _error(401, "unauthorized")


def _forward_payload(
    event: ParsedEvent,
    *,
    open_case_if_unmatched: bool = False,
    open_case_project_id: str | None = None,
) -> dict[str, Any]:
    """The ``trigger.ingest`` payload, with this caller's principal attached."""
    return ingest_payload(
        event,
        principal_kind=PRINCIPAL_KIND,
        principal_id=PRINCIPAL_ID,
        open_case_if_unmatched=open_case_if_unmatched,
        open_case_project_id=open_case_project_id,
    )


def _ingest_response(result: dict[str, Any]) -> JSONResponse:
    """202 for both a fresh event and a duplicate delivery (the gateway decides which)."""
    matched = result.get("matched_cases")
    return JSONResponse(
        {
            "accepted": bool(result.get("accepted", False)),
            "duplicate": bool(result.get("duplicate", False)),
            "event_id": str(result.get("event_id", "") or ""),
            "dedup_key": str(result.get("dedup_key", "") or ""),
            "matched_cases": [str(case) for case in matched] if isinstance(matched, list) else [],
            "unmatched_reason": result.get("unmatched_reason"),
            "opened_case_id": result.get("opened_case_id"),
        },
        status_code=202,
    )


# ── Endpoints ──────────────────────────────────────────────────────────


@router.post("/api/triggers/github")
async def github_webhook(request: Request) -> JSONResponse:
    """Ingest one GitHub webhook delivery.

    Refusal order is deliberate: secret configured, body bounded, signature
    verified, payload parsed -- only then does anything reach the gateway, so a
    forged or malformed delivery costs no gateway round-trip.
    """
    secret = os.environ.get(GITHUB_WEBHOOK_SECRET_ENV, "")
    if not secret:
        return _error(503, "webhook_secret_not_configured")
    declared = _declared_content_length(request)
    if declared is not None and declared > MAX_PAYLOAD_BYTES:
        return _error(413, "payload_too_large")
    body = await request.body()
    if not verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
        return _error(401, "invalid_signature")
    try:
        event = parse_webhook(request.headers, body)
    except PayloadTooLargeError:
        return _error(413, "payload_too_large")
    except WebhookPayloadError as exc:
        return _error(400, "invalid_payload", str(exc))
    if event is None:
        # Ignored event types still get a 2xx: GitHub retries non-2xx forever.
        return JSONResponse(
            {"accepted": False, "duplicate": False, "ignored": True}, status_code=202
        )
    result, failure = await _call(request, "trigger.ingest", _forward_payload(event))
    if failure is not None:
        return failure
    return _ingest_response(result)


@router.post("/api/triggers/ingest")
async def ingest_trigger(request: Request) -> JSONResponse:
    """Ingest a generic (api/cron/manual) event.

    The body is the trigger itself, not a raw source payload: callers that own
    a source (Lark, CI) reduce it to this shape first, exactly as the GitHub
    bridge does, so identity rules stay in one place. The accepted source/kind
    tokens are the trigger contract's; an event that needs a kind outside it is
    a contract change, not something this door should guess.
    """
    failure = _require_dashboard_token(request)
    if failure is not None:
        return failure
    try:
        document = await request.json()
    except ValueError:
        return _error(400, "invalid_json")
    if not isinstance(document, dict):
        return _error(400, "invalid_request", "body must be a JSON object")

    source = document.get("source")
    kind = document.get("kind")
    external_id = document.get("external_id")
    if not is_known_source(source):
        return _error(400, "unknown_source")
    if not is_known_kind(kind):
        return _error(400, "unknown_kind")
    if not isinstance(external_id, str) or not external_id.strip():
        return _error(400, "invalid_external_id")

    payload = document.get("payload", {})
    if not isinstance(payload, dict):
        return _error(400, "invalid_payload", "payload must be a JSON object")
    payload_json = canonical_payload_json(payload)
    if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return _error(413, "payload_too_large")

    thread_key = _optional_str(document.get("thread_key"))
    repo = _optional_str(document.get("repo"))
    actor = _optional_str(document.get("actor"))
    open_case_project_id = _optional_str(document.get("open_case_project_id"))
    open_case_if_unmatched = document.get("open_case_if_unmatched", False)
    if not isinstance(open_case_if_unmatched, bool):
        return _error(400, "invalid_request", "open_case_if_unmatched must be a boolean")

    event = ParsedEvent(
        source=str(source).strip().lower(),
        kind=str(kind).strip().lower(),
        external_id=external_id.strip(),
        payload=payload,
        thread_key=thread_key,
        repo=repo,
        actor=actor,
    )
    result, failure = await _call(
        request,
        "trigger.ingest",
        _forward_payload(
            event,
            open_case_if_unmatched=open_case_if_unmatched,
            open_case_project_id=open_case_project_id,
        ),
    )
    if failure is not None:
        return failure
    return _ingest_response(result)


@router.get("/api/triggers/subscriptions")
async def list_trigger_subscriptions(request: Request) -> JSONResponse:
    """Read the gateway's subscriptions, optionally scoped to one case."""
    payload: dict[str, Any] = {}
    case_id = _optional_str(request.query_params.get("case_id"))
    if case_id is not None:
        payload["case_id"] = case_id
    raw_limit = request.query_params.get("limit")
    if raw_limit is not None:
        if not raw_limit.strip().isdigit():
            return _error(400, "invalid_limit")
        limit = int(raw_limit.strip())
        if not 1 <= limit <= 1000:
            return _error(400, "invalid_limit")
        payload["limit"] = limit
    result, failure = await _call(request, "trigger.subscription.list", payload)
    if failure is not None:
        return failure
    subscriptions = result.get("subscriptions")
    return JSONResponse(
        {"subscriptions": subscriptions if isinstance(subscriptions, list) else []}
    )


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None
