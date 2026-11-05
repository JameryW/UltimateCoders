"""NATS request/reply client for the gateway governance plane.

The Rust gateway owns all governance state (submissions, outbox, work cases,
mandates, trigger inbox, attention items, proactive proposals). The Python
dashboard is a *projection*: it asks the gateway and renders the answer. It never
keeps a second copy of a case's authority, because two state authorities is
exactly the failure this architecture is built to avoid.

Transport is NATS request/reply::

    request  -> uc.governance.request   {"op": "case.create", "payload": {...}}
    response <- reply subject           {"ok": true,  "result": {...}}
                                        {"ok": false, "error": {"code","message"}}

Two exception types keep the failure modes distinguishable:

* :class:`GovernanceUnavailable` — the bridge is down / timed out. Callers answer
  HTTP 503; nothing is wrong with the request.
* :class:`GovernanceError` — the gateway rejected the operation (policy denial,
  illegal case transition, conflict). Callers answer 4xx with the code.

Honest limitation: this client does not authenticate the gateway. The NATS
connection is the trust boundary, which is why the bridge subscribes on a
cluster-internal subject and the deployment must not expose NATS publicly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Subject the gateway bridge subscribes to.
GOVERNANCE_SUBJECT = "uc.governance.request"

#: Default per-call timeout. The gateway answers from memory for most ops, so a
#: slow call means trouble rather than load.
DEFAULT_TIMEOUT_SECONDS = 5.0


class GovernanceError(Exception):
    """The gateway understood the operation and rejected it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class GovernanceUnavailable(Exception):  # noqa: N818 - public API name; Error suffix
    # would force every dashboard caller to rename a caught exception.
    """The gateway bridge could not be reached or did not answer in time."""


class GovernanceClient:
    """Thin typed wrapper over the gateway's governance ops."""

    def __init__(
        self,
        nats_client: Any = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        subject: str = GOVERNANCE_SUBJECT,
    ) -> None:
        self._nats = nats_client
        self._timeout = timeout
        self._subject = subject

    @property
    def is_connected(self) -> bool:
        return self._nats is not None

    def attach(self, nats_client: Any) -> None:
        """Attach (or replace) the NATS client, e.g. after a reconnect."""
        self._nats = nats_client

    async def call(self, op: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke one governance operation.

        Returns the ``result`` mapping. Raises :class:`GovernanceUnavailable` when
        the transport fails and :class:`GovernanceError` when the gateway denies
        the operation.
        """
        if self._nats is None:
            raise GovernanceUnavailable("governance bridge is not connected")
        envelope = {"op": op, "payload": payload or {}}
        try:
            encoded = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:  # pragma: no cover - programming error
            raise GovernanceError("invalid_request", str(exc)) from exc
        try:
            response = await asyncio.wait_for(
                self._nats.request(self._subject, encoded), timeout=self._timeout
            )
        except asyncio.TimeoutError as exc:
            raise GovernanceUnavailable(f"governance request {op} timed out") from exc
        except Exception as exc:  # nats errors, closed connection, …
            raise GovernanceUnavailable(f"governance request {op} failed: {exc}") from exc

        try:
            body = json.loads(response.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise GovernanceUnavailable(
                f"governance bridge returned a non-JSON reply for {op}"
            ) from exc

        if not isinstance(body, dict):
            raise GovernanceUnavailable("governance bridge returned a non-object reply")
        if not body.get("ok", False):
            error = body.get("error") or {}
            raise GovernanceError(
                str(error.get("code") or "governance_error"),
                str(error.get("message") or "operation rejected"),
            )
        result = body.get("result")
        return result if isinstance(result, dict) else {}

    # ── submissions (PR01) ────────────────────────────────────────────

    async def submit_task(
        self,
        description: str,
        project_id: str,
        *,
        submission_id: str | None = None,
        principal_kind: str = "service",
        principal_id: str = "dashboard",
        case_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "description": description,
            "project_id": project_id,
            "principal_kind": principal_kind,
            "principal_id": principal_id,
        }
        if submission_id:
            payload["submission_id"] = submission_id
        if case_id:
            payload["case_id"] = case_id
        return await self.call("submission.submit", payload)

    async def get_submission(self, submission_id: str) -> dict[str, Any]:
        return await self.call("submission.get", {"submission_id": submission_id})

    async def apply_submission(
        self,
        submission_id: str,
        task_id: str | None = None,
        *,
        principal: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Record that the orchestration exists (PR01's ``applied`` edge).

        Called by the consumer (the orchestrator) once its task was created. The
        gateway refuses the call without ``task_submit``, so ``principal`` names
        the consumer identity (``{"principal_kind": "agent", "principal_id":
        "uc-worker"}``); omit it only in tests that run against a permissive
        snapshot.
        """
        payload: dict[str, Any] = {"submission_id": submission_id}
        if task_id:
            payload["task_id"] = task_id
        if principal:
            payload.update(principal)
        return await self.call("submission.apply", payload)

    # ── cases + mandates (PR02) ───────────────────────────────────────

    async def create_case(self, **payload: Any) -> dict[str, Any]:
        return await self.call("case.create", payload)

    async def get_case(self, case_id: str) -> dict[str, Any]:
        return await self.call("case.get", {"case_id": case_id})

    async def list_cases(
        self,
        *,
        status: str | None = None,
        project_id: str | None = None,
        thread_key: str | None = None,
        limit: int = 50,
        include_terminal: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"limit": limit, "include_terminal": include_terminal}
        if status:
            payload["status"] = status
        if project_id:
            payload["project_id"] = project_id
        if thread_key:
            payload["thread_key"] = thread_key
        return await self.call("case.list", payload)

    async def transition_case(
        self,
        case_id: str,
        status: str,
        *,
        reason: str | None = None,
        principal_kind: str = "service",
        principal_id: str = "dashboard",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case_id": case_id,
            "status": status,
            "principal_kind": principal_kind,
            "principal_id": principal_id,
        }
        if reason:
            payload["reason"] = reason
        return await self.call("case.transition", payload)

    async def open_case_run(
        self,
        case_id: str,
        task_id: str,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"case_id": case_id, "task_id": task_id}
        if submission_id:
            payload["submission_id"] = submission_id
        return await self.call("case.run.open", payload)

    async def finish_case_run(
        self,
        case_id: str,
        run_id: str,
        outcome: str,
        detail: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case_id": case_id,
            "run_id": run_id,
            "outcome": outcome,
        }
        if detail:
            payload["detail"] = detail
        return await self.call("case.run.finish", payload)

    async def issue_mandate(
        self,
        case_id: str,
        scopes_json: str,
        *,
        expires_at: int | None = None,
        principal_kind: str = "service",
        principal_id: str = "dashboard",
        delegated_from: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case_id": case_id,
            "scopes_json": scopes_json,
            "principal_kind": principal_kind,
            "principal_id": principal_id,
        }
        if expires_at is not None:
            payload["expires_at"] = expires_at
        if delegated_from:
            payload["delegated_from"] = delegated_from
        return await self.call("mandate.issue", payload)

    async def revoke_mandate(
        self,
        mandate_id: str,
        reason: str,
        *,
        principal_kind: str = "service",
        principal_id: str = "dashboard",
    ) -> dict[str, Any]:
        return await self.call(
            "mandate.revoke",
            {
                "mandate_id": mandate_id,
                "reason": reason,
                "principal_kind": principal_kind,
                "principal_id": principal_id,
            },
        )

    async def get_mandate(
        self, *, mandate_id: str | None = None, case_id: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if mandate_id:
            payload["mandate_id"] = mandate_id
        if case_id:
            payload["case_id"] = case_id
        return await self.call("mandate.get", payload)

    async def authorize(self, **payload: Any) -> dict[str, Any]:
        return await self.call("authorize", payload)

    # ── triggers + subscriptions (PR04) ───────────────────────────────

    async def ingest_trigger(self, **payload: Any) -> dict[str, Any]:
        return await self.call("trigger.ingest", payload)

    async def list_subscriptions(
        self,
        *,
        case_id: str | None = None,
        source: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"limit": limit}
        if case_id:
            payload["case_id"] = case_id
        if source:
            payload["source"] = source
        return await self.call("trigger.subscription.list", payload)

    async def upsert_subscription(self, **payload: Any) -> dict[str, Any]:
        return await self.call("trigger.subscription.upsert", payload)

    async def delete_subscription(self, subscription_id: str) -> dict[str, Any]:
        return await self.call("trigger.subscription.delete", {"subscription_id": subscription_id})

    # ── attention + capacity (PR05) ───────────────────────────────────

    async def list_attention(
        self,
        *,
        include_resolved: bool = False,
        case_id: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"include_resolved": include_resolved, "limit": limit}
        if case_id:
            payload["case_id"] = case_id
        if kind:
            payload["kind"] = kind
        return await self.call("attention.list", payload)

    async def raise_attention(self, **payload: Any) -> dict[str, Any]:
        return await self.call("attention.raise", payload)

    async def resolve_attention(
        self,
        attention_id: str,
        resolution_kind: str,
        *,
        by: str | None = None,
        detail: str | None = None,
        principal_kind: str = "service",
        principal_id: str = "dashboard",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attention_id": attention_id,
            "resolution_kind": resolution_kind,
            "principal_kind": principal_kind,
            "principal_id": principal_id,
        }
        if by:
            payload["by"] = by
        if detail:
            payload["detail"] = detail
        return await self.call("attention.resolve", payload)

    async def get_capacity(self) -> dict[str, Any]:
        return await self.call("capacity.get", {})

    # ── proactive + watchdog (PR06) ───────────────────────────────────

    async def get_proactive_state(self) -> dict[str, Any]:
        return await self.call("proactive.state.get", {})

    async def set_proactive_mode(self, **payload: Any) -> dict[str, Any]:
        return await self.call("proactive.mode.set", payload)

    async def submit_proposal(self, **payload: Any) -> dict[str, Any]:
        return await self.call("proactive.proposal.submit", payload)

    async def list_proposals(
        self,
        *,
        status: str | None = None,
        case_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"limit": limit}
        if status:
            payload["status"] = status
        if case_id:
            payload["case_id"] = case_id
        return await self.call("proactive.proposal.list", payload)

    async def decide_proposal(self, **payload: Any) -> dict[str, Any]:
        return await self.call("proactive.proposal.decide", payload)

    async def list_proactive_audit(
        self,
        *,
        limit: int = 50,
        proposal_id: str | None = None,
        case_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"limit": limit}
        if proposal_id:
            payload["proposal_id"] = proposal_id
        if case_id:
            payload["case_id"] = case_id
        return await self.call("proactive.audit.list", payload)

    # ── approvals (PR03) ──────────────────────────────────────────────

    async def request_approval(
        self,
        *,
        tool: str,
        case_id: str | None = None,
        project_id: str | None = None,
        action_digest: str | None = None,
        reason: str | None = None,
        ttl_minutes: int | None = None,
        principal_kind: str = "agent",
        principal_id: str = "uc-worker",
    ) -> dict[str, Any]:
        """Ask a human to permit one call to a human-tier tool.

        The requester is an agent (or a service acting for one): a human does not
        request a permit to do their own work, and the gateway refuses a decision
        from the principal that asked. TTL defaults to the gateway's own
        ``APPROVAL_TTL_MINUTES``.
        """
        payload: dict[str, Any] = {
            "tool": tool,
            "principal_kind": principal_kind,
            "principal_id": principal_id,
        }
        for name, value in (
            ("case_id", case_id),
            ("project_id", project_id),
            ("action_digest", action_digest),
            ("reason", reason),
        ):
            if value:
                payload[name] = value
        if ttl_minutes is not None:
            payload["ttl_minutes"] = int(ttl_minutes)
        return await self.call("approval.request", payload)

    async def list_approvals(
        self,
        *,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"limit": limit, "offset": offset}
        if state:
            payload["state"] = state
        return await self.call("approval.list", payload)

    async def decide_approval(self, **payload: Any) -> dict[str, Any]:
        return await self.call("approval.decide", payload)

    async def report_stall(self, **payload: Any) -> dict[str, Any]:
        return await self.call("watchdog.stall.report", payload)


_DEFAULT_CLIENT: GovernanceClient | None = None


def configure_default_client(client: GovernanceClient) -> GovernanceClient:
    """Install the process-wide client used when a handler has none injected."""
    global _DEFAULT_CLIENT
    _DEFAULT_CLIENT = client
    return client


def get_default_governance_client() -> GovernanceClient | None:
    return _DEFAULT_CLIENT


def reset_default_client() -> None:
    """Clear the process-wide client (tests)."""
    global _DEFAULT_CLIENT
    _DEFAULT_CLIENT = None


def summarize_capacity(capacity: dict[str, Any]) -> dict[str, int]:
    """Normalize a capacity mapping into the ints the UI relies on.

    Missing keys mean "not reported", which is not the same as zero — but for
    capacity the UI needs a number, so absent becomes 0 and the caller can tell
    from the raw mapping whether the gateway answered at all.
    """
    keys = (
        "worker_slots_total",
        "worker_slots_busy",
        "worker_slots_reserved_human",
        "waiting_event_cases",
        "verifying_cases",
        "needs_human_items",
        "blocked_cases",
        "worker_slots_available",
        "slots_saved_by_quiet_waiting",
    )
    return {key: int(capacity.get(key) or 0) for key in keys}


__all__: list[str] = [
    "GOVERNANCE_SUBJECT",
    "DEFAULT_TIMEOUT_SECONDS",
    "GovernanceClient",
    "GovernanceError",
    "GovernanceUnavailable",
    "configure_default_client",
    "get_default_governance_client",
    "reset_default_client",
    "summarize_capacity",
]
