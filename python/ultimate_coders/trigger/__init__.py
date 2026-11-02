"""Trigger sources: how external events become one deduplicated inbox record.

The trigger inbox is the only door for external events, and every event is
reduced to a deterministic identity before it touches anything durable:
``(source, kind, external_id, canonical payload)``. The gateway owns the dedup
key, the subscriptions and the case routing; this package owns the part that
must be right *before* the gateway sees the event -- authenticating a delivery,
mapping a GitHub payload to a trigger kind, deriving the thread
(``owner/repo#N``) and keeping the webhook and polling paths in agreement.

Triggers carry no authority: an event can ask for work, but it cannot name a
mandate, raise a budget or pick a tool. That is why nothing in this package
consults permissions -- the API layer authenticates the *delivery*, and the
gateway decides what the event may cause.

``github_webhook`` is pure (no I/O): signature verification, payload parsing
and the canonical event shape. ``github_poller`` is the reconciliation
backstop; its transport is injected so this package adds no HTTP dependency.
"""

from ultimate_coders.trigger.github_poller import (
    CURSOR_NAMESPACE,
    GitHubReconciler,
    InvalidPollPageError,
    event_from_poll_entry,
)
from ultimate_coders.trigger.github_webhook import (
    KIND_CHECK_RUN,
    KIND_ISSUE_COMMENT,
    KIND_ISSUE_OPENED,
    KIND_PULL_REQUEST_COMMENT,
    KIND_PULL_REQUEST_OPENED,
    KIND_PULL_REQUEST_REVIEW,
    KIND_PULL_REQUEST_SYNCHRONIZE,
    KIND_PUSH,
    KIND_TOKENS,
    KIND_WORKFLOW_RUN,
    MAX_PAYLOAD_BYTES,
    SOURCE_GITHUB,
    SOURCE_TOKENS,
    InvalidPayloadError,
    ParsedEvent,
    PayloadTooLargeError,
    WebhookPayloadError,
    canonical_payload_json,
    event_from_body,
    ingest_payload,
    is_known_kind,
    is_known_source,
    normalize_thread_key,
    parse_webhook,
    verify_signature,
)

__all__ = [
    "CURSOR_NAMESPACE",
    "GitHubReconciler",
    "InvalidPayloadError",
    "InvalidPollPageError",
    "KIND_CHECK_RUN",
    "KIND_ISSUE_COMMENT",
    "KIND_ISSUE_OPENED",
    "KIND_PULL_REQUEST_COMMENT",
    "KIND_PULL_REQUEST_OPENED",
    "KIND_PULL_REQUEST_REVIEW",
    "KIND_PULL_REQUEST_SYNCHRONIZE",
    "KIND_PUSH",
    "KIND_TOKENS",
    "KIND_WORKFLOW_RUN",
    "MAX_PAYLOAD_BYTES",
    "ParsedEvent",
    "PayloadTooLargeError",
    "SOURCE_GITHUB",
    "SOURCE_TOKENS",
    "WebhookPayloadError",
    "canonical_payload_json",
    "event_from_body",
    "event_from_poll_entry",
    "ingest_payload",
    "is_known_kind",
    "is_known_source",
    "normalize_thread_key",
    "parse_webhook",
    "verify_signature",
]
