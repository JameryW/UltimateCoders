"""GitHub webhook deliveries reduced to canonical trigger events.

The gateway derives the trigger dedup key from exactly four values -- ``source``,
``kind``, ``external_id`` and a digest of the payload we forward. That makes
identity a Python responsibility: GitHub delivers at least once and a polling
reconciler races those deliveries, so both paths must reduce the same GitHub
event to the same four values or the inbox records it twice.

Invariants:

* ``external_id`` is a source-native identity (issue/comment/review/check-run
  id, or the pushed commit), never the delivery id. A delivery id exists only
  for webhooks, so folding it into identity would guarantee that a polled twin
  never dedupes against its webhook delivery.
* ``payload`` is a canonical projection holding only identity and status
  fields. It is deliberately not the raw body: the digest is taken over what
  we forward, and volatile fields (URLs, timestamps, rendered HTML) differ
  between the webhook body and the events-API entry describing the same event.
* An event that cannot be given a stable identity or thread is rejected with
  ``InvalidPayloadError`` rather than guessed. A guessed identity either
  collapses distinct events or duplicates one, and neither is recoverable
  downstream.

Honest limitations: only the event types in ``SUPPORTED_EVENTS`` and the
actions that have a kind token are mapped; everything else parses to ``None``
(ignored) so a missing mapping shows up as a gap instead of a mislabelled
trigger. The full delivery body is not retained -- callers that need it for
audit must store it before parsing. Signature verification lives in the API
layer (``verify_signature``); parsing itself authenticates nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

SOURCE_GITHUB = "github"

#: Source tokens the gateway accepts (uc-types ``SourceKind``).
SOURCE_TOKENS = frozenset({"github", "ci", "lark", "dashboard", "api", "cron", "manual"})

#: Trigger kinds this bridge can produce (uc-types ``TriggerKind``).
KIND_ISSUE_OPENED = "issue_opened"
KIND_ISSUE_COMMENT = "issue_comment"
KIND_PULL_REQUEST_OPENED = "pull_request_opened"
KIND_PULL_REQUEST_SYNCHRONIZE = "pull_request_synchronize"
KIND_PULL_REQUEST_REVIEW = "pull_request_review"
KIND_PULL_REQUEST_COMMENT = "pull_request_comment"
KIND_PUSH = "push"
KIND_CHECK_RUN = "check_run"
KIND_WORKFLOW_RUN = "workflow_run"

KIND_TOKENS = frozenset(
    {
        KIND_ISSUE_OPENED,
        KIND_ISSUE_COMMENT,
        KIND_PULL_REQUEST_OPENED,
        KIND_PULL_REQUEST_SYNCHRONIZE,
        KIND_PULL_REQUEST_REVIEW,
        KIND_PULL_REQUEST_COMMENT,
        KIND_PUSH,
        KIND_CHECK_RUN,
        KIND_WORKFLOW_RUN,
    }
)

#: Kinds whose thread is a numbered GitHub object (``owner/repo#N``). ``push``
#: has no number -- its thread is a commit (``owner/repo@sha``) -- so it is not
#: listed here and ``normalize_thread_key`` refuses it.
NUMBERED_THREAD_KINDS = KIND_TOKENS - {KIND_PUSH}

#: GitHub caps webhook bodies well below this; anything larger is not a
#: delivery we should spend memory parsing.
MAX_PAYLOAD_BYTES = 1024 * 1024

EVENT_HEADER = "x-github-event"
DELIVERY_HEADER = "x-github-delivery"
SIGNATURE_HEADER = "x-hub-signature-256"
SIGNATURE_PREFIX = "sha256="


class WebhookPayloadError(ValueError):
    """A delivery that cannot be reduced to a canonical trigger event."""


class InvalidPayloadError(WebhookPayloadError):
    """The body is not JSON, not an object, or lacks required identity fields."""


class PayloadTooLargeError(WebhookPayloadError):
    """The delivery exceeds ``MAX_PAYLOAD_BYTES``; it is refused unparsed."""


@dataclass(frozen=True)
class ParsedEvent:
    """One source event, in the shape the gateway ingests.

    ``delivery_id`` is transport metadata only: it is forwarded for tracing but
    never contributes to identity, so a polled event (which has no delivery id)
    still converges with its webhook delivery.
    """

    source: str
    kind: str
    external_id: str
    payload: dict[str, Any]
    thread_key: str | None = None
    repo: str | None = None
    actor: str | None = None
    delivery_id: str | None = None
    event_name: str = ""


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check GitHub's ``X-Hub-Signature-256`` value against the raw body.

    The comparison is constant time (``hmac.compare_digest`` on bytes, so a
    non-ASCII header cannot raise). An empty secret or a missing/short header
    is a refusal, not a bypass: callers decide separately whether an
    unconfigured secret is acceptable, and for the webhook endpoint it is not.
    """
    if not secret or not header or not isinstance(header, str):
        return False
    if not header.startswith(SIGNATURE_PREFIX):
        return False
    supplied = header[len(SIGNATURE_PREFIX) :].strip().lower()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), supplied.encode("utf-8", "replace"))


def canonical_payload_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON for the gateway.

    Sorted keys and no incidental separators. The gateway re-serialises the
    payload object it receives to derive the digest, so this is not the dedup
    rule itself -- it is the wire form, kept canonical so the webhook and poller
    paths cannot disagree because of Python-side dict ordering.
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ingest_payload(
    event: ParsedEvent,
    *,
    principal_kind: str = "service",
    principal_id: str = "dashboard",
    open_case_if_unmatched: bool = False,
    open_case_project_id: str | None = None,
) -> dict[str, Any]:
    """The one ``trigger.ingest`` payload shape for a canonical event.

    The webhook API and anything forwarding polled events both build their
    request here, so the gateway's dedup input cannot differ between the two
    paths. ``principal_*`` names the authenticated *caller* (a service), never
    the event: a trigger holds no authority.
    """
    payload = {
        "source": event.source,
        "kind": event.kind,
        "external_id": event.external_id,
        "thread_key": event.thread_key,
        "repo": event.repo,
        "actor": event.actor,
        "payload_json": canonical_payload_json(event.payload),
        "delivery_id": event.delivery_id,
        "principal_kind": principal_kind,
        "principal_id": principal_id,
        "open_case_if_unmatched": open_case_if_unmatched,
        "open_case_project_id": open_case_project_id,
    }
    return {key: value for key, value in payload.items() if value is not None}


def normalize_thread_key(repo: str, kind: str, number: int) -> str:
    """Return the canonical thread of a numbered GitHub object: ``owner/repo#N``.

    ``kind`` is part of the signature so that a kind without a numbered thread
    is refused here instead of silently collapsing every such event onto one
    key (a push would otherwise become ``owner/repo#0``).
    """
    if kind not in NUMBERED_THREAD_KINDS:
        raise ValueError(f"{kind!r} has no numbered thread; use a commit thread")
    if not isinstance(repo, str) or "/" not in repo.strip():
        raise ValueError("repo must be an 'owner/name' string")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError("thread number must be a positive integer")
    return f"{repo.strip()}#{number}"


def parse_webhook(headers: Mapping[str, str], body: bytes) -> ParsedEvent | None:
    """Reduce one GitHub webhook delivery to a canonical event.

    Returns ``None`` for an event type we do not map (including ``ping`` and
    any action without a kind token). Raises ``PayloadTooLargeError`` before
    parsing an oversized body and ``InvalidPayloadError`` for a body that is not
    a JSON object or a delivery without an event header.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise InvalidPayloadError("webhook body must be bytes")
    if len(body) > MAX_PAYLOAD_BYTES:
        raise PayloadTooLargeError(f"webhook body exceeds {MAX_PAYLOAD_BYTES} bytes")
    event_name = _header(headers, EVENT_HEADER)
    if not event_name:
        raise InvalidPayloadError(f"missing {EVENT_HEADER} header")
    try:
        document = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidPayloadError("webhook body is not UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise InvalidPayloadError("webhook body must be a JSON object")
    return event_from_body(
        event_name,
        document,
        delivery_id=_header(headers, DELIVERY_HEADER),
    )


def event_from_body(
    event_name: str,
    body: Mapping[str, Any],
    *,
    delivery_id: str | None = None,
    repo: str | None = None,
    actor: str | None = None,
) -> ParsedEvent | None:
    """Map one GitHub event payload to a canonical event (shared by webhook and poller).

    ``repo``/``actor`` are supplied explicitly when the caller has a
    representation whose wrapper differs (the events API puts them next to the
    payload rather than inside it); otherwise they are read from the body.
    """
    if not isinstance(event_name, str):
        raise InvalidPayloadError("event name must be a string")
    handler = _HANDLERS.get(event_name.strip().lower())
    if handler is None:
        return None
    return handler(body, delivery_id, repo, actor)


def is_known_source(value: Any) -> bool:
    """Whether ``value`` is a source token the gateway accepts."""
    return isinstance(value, str) and value.strip().lower() in SOURCE_TOKENS


def is_known_kind(value: Any) -> bool:
    """Whether ``value`` is a trigger kind token the gateway accepts."""
    return isinstance(value, str) and value.strip().lower() in KIND_TOKENS


# ── Normalisers ────────────────────────────────────────────────────────


def _parse_issues(
    body: Mapping[str, Any],
    delivery_id: str | None,
    repo: str | None,
    actor: str | None,
) -> ParsedEvent | None:
    action = _as_str(body.get("action"))
    if action != "opened":
        return None
    issue = _mapping(body.get("issue"))
    number = _as_int(issue.get("number"))
    issue_id = _as_int(issue.get("id"))
    if number is None or issue_id is None:
        raise InvalidPayloadError("issues event without issue number or id")
    repo = _resolve_repo(body, repo)
    payload = _compact({"action": "opened", "number": number, "issue_id": issue_id})
    return _numbered_event(
        KIND_ISSUE_OPENED,
        f"issue:{issue_id}",
        repo,
        number,
        payload,
        delivery_id,
        actor or _actor_from_body(body),
        "issues",
    )


def _parse_issue_comment(
    body: Mapping[str, Any],
    delivery_id: str | None,
    repo: str | None,
    actor: str | None,
) -> ParsedEvent | None:
    action = _as_str(body.get("action"))
    if action != "created":
        return None
    issue = _mapping(body.get("issue"))
    comment = _mapping(body.get("comment"))
    number = _as_int(issue.get("number"))
    comment_id = _as_int(comment.get("id"))
    if number is None or comment_id is None:
        raise InvalidPayloadError("issue_comment event without issue number or comment id")
    repo = _resolve_repo(body, repo)
    # GitHub reports PR comments as issue comments; the presence of the
    # ``pull_request`` marker on the issue is what distinguishes the thread kind.
    kind = (
        KIND_PULL_REQUEST_COMMENT
        if issue.get("pull_request") is not None
        else KIND_ISSUE_COMMENT
    )
    payload = _compact({"action": "created", "number": number, "comment_id": comment_id})
    return _numbered_event(
        kind,
        f"comment:{comment_id}",
        repo,
        number,
        payload,
        delivery_id,
        actor or _actor_from_body(body),
        "issue_comment",
    )


def _parse_pull_request(
    body: Mapping[str, Any],
    delivery_id: str | None,
    repo: str | None,
    actor: str | None,
) -> ParsedEvent | None:
    action = _as_str(body.get("action"))
    # Ignored actions are answered before identity is required: an event we do
    # not map is not an error, whatever else it is missing.
    if action not in ("opened", "synchronize"):
        return None
    pull = _mapping(body.get("pull_request"))
    number = _as_int(pull.get("number")) or _as_int(body.get("number"))
    pull_id = _as_int(pull.get("id"))
    if number is None or pull_id is None:
        raise InvalidPayloadError("pull_request event without PR number or id")
    repo = _resolve_repo(body, repo)
    sha = _head_sha(pull)
    if action == "opened":
        kind = KIND_PULL_REQUEST_OPENED
        external_id = f"pull_request:{pull_id}:opened"
    else:
        if sha is None:
            raise InvalidPayloadError("pull_request synchronize event without head sha")
        kind = KIND_PULL_REQUEST_SYNCHRONIZE
        external_id = f"pull_request:{pull_id}:{sha}"
    payload = _compact(
        {"action": action, "number": number, "pull_request_id": pull_id, "sha": sha}
    )
    return _numbered_event(
        kind,
        external_id,
        repo,
        number,
        payload,
        delivery_id,
        actor or _actor_from_body(body),
        "pull_request",
    )


def _parse_pull_request_review(
    body: Mapping[str, Any],
    delivery_id: str | None,
    repo: str | None,
    actor: str | None,
) -> ParsedEvent | None:
    action = _as_str(body.get("action"))
    if action != "submitted":
        return None
    review = _mapping(body.get("review"))
    pull = _mapping(body.get("pull_request"))
    review_id = _as_int(review.get("id"))
    number = _as_int(pull.get("number"))
    if review_id is None or number is None:
        raise InvalidPayloadError("pull_request_review event without review id or PR number")
    repo = _resolve_repo(body, repo)
    payload = _compact(
        {
            "action": "submitted",
            "number": number,
            "review_id": review_id,
            "state": _as_str(review.get("state")),
            "sha": _head_sha(pull),
        }
    )
    return _numbered_event(
        KIND_PULL_REQUEST_REVIEW,
        f"review:{review_id}",
        repo,
        number,
        payload,
        delivery_id,
        actor or _actor_from_body(body),
        "pull_request_review",
    )


def _parse_push(
    body: Mapping[str, Any],
    delivery_id: str | None,
    repo: str | None,
    actor: str | None,
) -> ParsedEvent | None:
    ref = _as_str(body.get("ref"))
    # Webhook bodies call the head commit ``after``; the events API calls it
    # ``head``. Same commit, so accept both spellings.
    after = _as_str(body.get("after")) or _as_str(body.get("head"))
    if ref is None or after is None:
        raise InvalidPayloadError("push event without ref or head commit")
    repo = _resolve_repo(body, repo)
    payload = _compact({"ref": ref, "before": _as_str(body.get("before")), "after": after})
    # A deleted branch has no commit to thread on; linking every deletion to the
    # all-zero sha would collapse unrelated branches onto one thread.
    deleted = bool(body.get("deleted")) or set(after) == {"0"}
    return ParsedEvent(
        source=SOURCE_GITHUB,
        kind=KIND_PUSH,
        external_id=f"push:{ref}:{after}",
        payload=payload,
        thread_key=None if deleted else f"{repo}@{after}",
        repo=repo,
        actor=actor or _actor_from_body(body),
        delivery_id=delivery_id,
        event_name="push",
    )


def _parse_check_run(
    body: Mapping[str, Any],
    delivery_id: str | None,
    repo: str | None,
    actor: str | None,
) -> ParsedEvent | None:
    run = _mapping(body.get("check_run"))
    run_id = _as_int(run.get("id"))
    if run_id is None:
        raise InvalidPayloadError("check_run event without check run id")
    repo = _resolve_repo(body, repo)
    status = _as_str(run.get("status"))
    conclusion = _as_str(run.get("conclusion"))
    sha = _as_str(run.get("head_sha"))
    number, thread_key = _linked_thread(repo, run, KIND_CHECK_RUN)
    payload = _compact(
        {
            "action": _as_str(body.get("action")),
            "check_run_id": run_id,
            "status": status,
            "conclusion": conclusion,
            "number": number,
            "sha": sha,
        }
    )
    return ParsedEvent(
        source=SOURCE_GITHUB,
        kind=KIND_CHECK_RUN,
        external_id=f"check_run:{run_id}:{status or ''}:{conclusion or ''}",
        payload=payload,
        thread_key=thread_key or _commit_thread(repo, sha),
        repo=repo,
        actor=actor or _actor_from_body(body),
        delivery_id=delivery_id,
        event_name="check_run",
    )


def _parse_workflow_run(
    body: Mapping[str, Any],
    delivery_id: str | None,
    repo: str | None,
    actor: str | None,
) -> ParsedEvent | None:
    run = _mapping(body.get("workflow_run"))
    run_id = _as_int(run.get("id"))
    if run_id is None:
        raise InvalidPayloadError("workflow_run event without workflow run id")
    repo = _resolve_repo(body, repo)
    status = _as_str(run.get("status"))
    conclusion = _as_str(run.get("conclusion"))
    sha = _as_str(run.get("head_sha"))
    number, thread_key = _linked_thread(repo, run, KIND_WORKFLOW_RUN)
    payload = _compact(
        {
            "action": _as_str(body.get("action")),
            "workflow_run_id": run_id,
            "status": status,
            "conclusion": conclusion,
            "number": number,
            "sha": sha,
        }
    )
    return ParsedEvent(
        source=SOURCE_GITHUB,
        kind=KIND_WORKFLOW_RUN,
        external_id=f"workflow_run:{run_id}:{status or ''}:{conclusion or ''}",
        payload=payload,
        thread_key=thread_key or _commit_thread(repo, sha),
        repo=repo,
        actor=actor or _actor_from_body(body),
        delivery_id=delivery_id,
        event_name="workflow_run",
    )


_HANDLERS: dict[str, Callable[..., ParsedEvent | None]] = {
    "issues": _parse_issues,
    "issue_comment": _parse_issue_comment,
    "pull_request": _parse_pull_request,
    "pull_request_review": _parse_pull_request_review,
    "push": _parse_push,
    "check_run": _parse_check_run,
    "workflow_run": _parse_workflow_run,
}

#: Event types this bridge maps. Anything absent is ignored, not mislabelled.
SUPPORTED_EVENTS = frozenset(_HANDLERS)


# ── Small pure helpers ─────────────────────────────────────────────────


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (a plain dict is not case-insensitive)."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return _as_str(value)
    return None


def _numbered_event(
    kind: str,
    external_id: str,
    repo: str,
    number: int,
    payload: dict[str, Any],
    delivery_id: str | None,
    actor: str | None,
    event_name: str,
) -> ParsedEvent:
    return ParsedEvent(
        source=SOURCE_GITHUB,
        kind=kind,
        external_id=external_id,
        payload=payload,
        thread_key=normalize_thread_key(repo, kind, number),
        repo=repo,
        actor=actor,
        delivery_id=delivery_id,
        event_name=event_name,
    )


def _linked_thread(
    repo: str, run: Mapping[str, Any], kind: str
) -> tuple[int | None, str | None]:
    """Thread of a check/workflow run: its PR when linked, else no thread here."""
    pull_requests = run.get("pull_requests")
    if isinstance(pull_requests, list):
        for entry in pull_requests:
            number = _as_int(_mapping(entry).get("number"))
            if number is not None:
                return number, normalize_thread_key(repo, kind, number)
    return None, None


def _commit_thread(repo: str, sha: str | None) -> str | None:
    return f"{repo}@{sha}" if sha else None


def _resolve_repo(body: Mapping[str, Any], repo: str | None) -> str:
    resolved = _as_str(repo) or _repo_from_body(body)
    if resolved is None or "/" not in resolved:
        raise InvalidPayloadError("event has no repository identity (owner/name)")
    return resolved


def _repo_from_body(body: Mapping[str, Any]) -> str | None:
    return _as_str(_mapping(body.get("repository")).get("full_name"))


def _actor_from_body(body: Mapping[str, Any]) -> str | None:
    return _as_str(_mapping(body.get("sender")).get("login"))


def _head_sha(pull: Mapping[str, Any]) -> str | None:
    return _as_str(_mapping(pull.get("head")).get("sha"))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop absent fields so two representations of one event serialise alike."""
    return {key: value for key, value in payload.items() if value is not None}


def _as_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
