"""Polling reconciler for GitHub sources.

Webhooks are at-least-once and can be lost entirely; a periodic poll is the
backstop that notices what the delivery path missed. The invariant is that a
polled event is identical -- in every field the gateway's dedup key is derived
from -- to its webhook twin. ``GitHubReconciler`` therefore reuses
``event_from_body`` from ``github_webhook`` instead of re-implementing the
mapping, and it never invents a ``delivery_id``: a delivery id is transport
metadata, so mixing it into a polled event would defeat the very dedup this
exists to achieve.

Honest limitations:

* Transport is injected. The caller supplies an async ``fetch``; this module
  imports no HTTP client, and conditional requests are the caller's job. The
  reconciler only forwards the stored cursor/etag and trusts the returned page.
* The cursor advances monotonically by GitHub event id. Unsupported event
  types are skipped *after* the cursor passes them, so enabling a new mapping
  later does not backfill history -- the events API only serves recent events
  anyway.
* Polling is bounded: at most ``max_items`` events per repo per poll (oldest
  first, so a backlog drains instead of being silently dropped), and a fetch
  failure records exponential backoff instead of spinning. A poll attempted
  while a repo is still backing off returns no events without calling fetch.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from ultimate_coders.runtime_state import RuntimeState
from ultimate_coders.trigger.github_webhook import (
    ParsedEvent,
    WebhookPayloadError,
    event_from_body,
)

logger = logging.getLogger(__name__)

#: Runtime-state namespace for reconciliation cursors (one record per repo).
CURSOR_NAMESPACE = "trigger_cursor"

DEFAULT_MAX_ITEMS = 50
DEFAULT_BACKOFF_BASE_SECONDS = 5.0
DEFAULT_BACKOFF_MAX_SECONDS = 300.0

#: GitHub events-API type names → the webhook event name ``event_from_body``
#: dispatches on. Only mapped types are reconciled; everything else is skipped.
EVENT_TYPE_NAMES: dict[str, str] = {
    "IssuesEvent": "issues",
    "IssueCommentEvent": "issue_comment",
    "PullRequestEvent": "pull_request",
    "PullRequestReviewEvent": "pull_request_review",
    "PushEvent": "push",
    "CheckRunEvent": "check_run",
    "WorkflowRunEvent": "workflow_run",
}


class InvalidPollPageError(ValueError):
    """The injected transport returned something that is not an events page."""


def event_from_poll_entry(entry: Mapping[str, Any]) -> ParsedEvent | None:
    """Reduce one events-API entry to the same canonical event a webhook would.

    Returns ``None`` for event types we do not map. Raises ``WebhookPayloadError``
    for a mapped type whose payload is malformed, so the caller can count it as
    skipped instead of mistaking it for "nothing happened".
    """
    event_type = entry.get("type")
    event_name = EVENT_TYPE_NAMES.get(event_type) if isinstance(event_type, str) else None
    if event_name is None:
        return None
    payload = entry.get("payload")
    if not isinstance(payload, Mapping):
        raise WebhookPayloadError(f"{event_type} entry has no payload object")
    return event_from_body(
        event_name,
        payload,
        delivery_id=None,
        repo=_entry_repo(entry),
        actor=_entry_actor(entry),
    )


class GitHubReconciler:
    """Bounded, cursor-driven poller for one or more GitHub repositories.

    ``fetch`` is awaited as ``fetch(repo, etag=..., cursor=...)`` and must
    return the decoded JSON page: ``{"items": [<events-API entries>], "etag":
    "<validator or empty>"}``. ``cursor`` is the last processed event id (a
    string, or ``None`` before the first successful poll) so the transport can
    add paging/``since`` if it wants; the reconciler does not depend on it.
    """

    def __init__(
        self,
        fetch: Callable[..., Awaitable[Mapping[str, Any]]],
        repos: Sequence[str],
        *,
        runtime_state: RuntimeState | None = None,
        max_items: int = DEFAULT_MAX_ITEMS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not callable(fetch):
            raise TypeError("fetch must be an async callable")
        if isinstance(repos, str):
            raise TypeError("repos must be a sequence of 'owner/name' strings")
        normalized: list[str] = []
        for repo in repos:
            if not isinstance(repo, str):
                raise TypeError("every repo must be an 'owner/name' string")
            name = repo.strip()
            if not name:
                continue
            if "/" not in name:
                raise ValueError(f"repo must be 'owner/name': {repo!r}")
            if name not in normalized:
                normalized.append(name)
        if not normalized:
            raise ValueError("at least one repository is required")
        if isinstance(max_items, bool) or not isinstance(max_items, int):
            raise TypeError("max_items must be an integer")
        if not 1 <= max_items <= 200:
            raise ValueError("max_items must be between 1 and 200")
        if backoff_base_seconds <= 0:
            raise ValueError("backoff_base_seconds must be positive")
        if backoff_max_seconds < backoff_base_seconds:
            raise ValueError("backoff_max_seconds must not be below the base delay")

        self._fetch = fetch
        self._repos = tuple(normalized)
        self._state = runtime_state if runtime_state is not None else RuntimeState()
        self._max_items = max_items
        self._backoff_base = float(backoff_base_seconds)
        self._backoff_max = float(backoff_max_seconds)
        self._clock = clock if clock is not None else time.time

    @property
    def repos(self) -> tuple[str, ...]:
        """Repositories polled by ``poll``, in the order given at construction."""
        return self._repos

    def cursor(self, repo: str) -> dict[str, Any]:
        """The persisted reconciliation record for ``repo`` (empty before first poll)."""
        return self._state.get(CURSOR_NAMESPACE, repo) or {}

    async def poll_once(self, repo: str) -> list[ParsedEvent]:
        """Poll one repository once; returns new events oldest-first.

        Returns ``[]`` without touching the transport while the repo is still in
        its recorded backoff window. A transport or page-shape failure is
        recorded on the cursor (``failures``, ``next_poll_at``, ``last_error``)
        and then re-raised: the caller owns the decision to keep polling.
        """
        record = self.cursor(repo)
        now = self._clock()
        next_poll_at = _as_float(record.get("next_poll_at"))
        if next_poll_at is not None and next_poll_at > now:
            return []
        try:
            page = await self._fetch(
                repo,
                etag=_as_str(record.get("etag")),
                cursor=_as_str(record.get("last_event_id")),
            )
            items, etag = _page_items(page)
        except Exception as exc:
            self._record_failure(repo, now, exc)
            raise
        return self._apply_items(repo, record, now, items, etag)

    async def poll(self) -> list[ParsedEvent]:
        """Poll every configured repository, newest cursor first.

        One unreachable repository must not stall the others: its failure is
        recorded on its own cursor record and the loop continues.
        """
        events: list[ParsedEvent] = []
        for repo in self._repos:
            try:
                events.extend(await self.poll_once(repo))
            except Exception:
                logger.warning("trigger poll failed for %s", repo, exc_info=True)
        return events

    def _apply_items(
        self,
        repo: str,
        record: Mapping[str, Any],
        now: float,
        items: Sequence[Any],
        etag: str | None,
    ) -> list[ParsedEvent]:
        cursor_id = _as_event_id(record.get("last_event_id"))
        pending: dict[int, Mapping[str, Any]] = {}
        for entry in items:
            if not isinstance(entry, Mapping):
                continue
            event_id = _as_event_id(entry.get("id"))
            if event_id is None:
                continue
            if cursor_id is not None and event_id <= cursor_id:
                continue
            # GitHub can repeat an event across pages; identity is the id.
            pending.setdefault(event_id, entry)

        ordered = sorted(pending.items())[: self._max_items]
        events: list[ParsedEvent] = []
        skipped = 0
        newest = cursor_id
        for event_id, entry in ordered:
            newest = event_id
            try:
                parsed = event_from_poll_entry(entry)
            except WebhookPayloadError:
                skipped += 1
                parsed = None
            if parsed is not None:
                events.append(parsed)
        self._record_success(repo, now, etag, newest, skipped)
        return events

    def _record_success(
        self,
        repo: str,
        now: float,
        etag: str | None,
        last_event_id: int | None,
        skipped: int,
    ) -> None:
        def change(current: dict[str, Any]) -> dict[str, Any]:
            return {
                **current,
                "repo": repo,
                # No validator on this page means the next request must be
                # unconditional; keeping a stale etag would invite a wrong 304.
                "etag": etag or "",
                "last_event_id": (
                    str(last_event_id)
                    if last_event_id is not None
                    else str(current.get("last_event_id", ""))
                ),
                "failures": 0,
                "next_poll_at": 0.0,
                "last_polled_at": now,
                "last_skipped": skipped,
                "last_error": "",
            }

        self._state.mutate(CURSOR_NAMESPACE, repo, change)

    def _record_failure(self, repo: str, now: float, exc: BaseException) -> None:
        def change(current: dict[str, Any]) -> dict[str, Any]:
            failures = int(current.get("failures", 0) or 0) + 1
            delay = min(self._backoff_max, self._backoff_base * (2 ** (failures - 1)))
            return {
                **current,
                "repo": repo,
                "failures": failures,
                "next_poll_at": now + delay,
                "last_polled_at": now,
                "last_error": f"{type(exc).__name__}: {exc}"[:500],
            }

        self._state.mutate(CURSOR_NAMESPACE, repo, change)


def _page_items(page: Any) -> tuple[list[Any], str | None]:
    if not isinstance(page, Mapping):
        raise InvalidPollPageError("fetch must return a JSON object with an 'items' list")
    items = page.get("items")
    if not isinstance(items, list):
        raise InvalidPollPageError("fetch page is missing the 'items' list")
    return items, _as_str(page.get("etag"))


def _entry_repo(entry: Mapping[str, Any]) -> str | None:
    repo = entry.get("repo")
    if isinstance(repo, Mapping):
        return _as_str(repo.get("name"))
    return None


def _entry_actor(entry: Mapping[str, Any]) -> str | None:
    actor = entry.get("actor")
    if isinstance(actor, Mapping):
        return _as_str(actor.get("login"))
    return None


def _as_event_id(value: Any) -> int | None:
    """GitHub event ids are increasing integers (served as strings)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None
