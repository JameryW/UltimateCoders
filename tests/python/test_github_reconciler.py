"""PR04 -- polling reconciler converges with the webhook path.

Acceptance under test: a polled event and its webhook twin reduce to the same
external id and the same canonical payload, so the gateway's dedup key (a
sha256 over ``source|kind|external_id|payload_digest``) collapses them into one
inbox record. The rule is re-implemented locally here on purpose -- the test
must prove what Python forwards, not call into the Rust implementation.

The other half of the contract is boundedness: at most N events per poll,
oldest first, with exponential backoff recorded through ``runtime_state`` so a
flapping transport is not hammered.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from ultimate_coders.runtime_state import RuntimeState
from ultimate_coders.trigger.github_poller import (
    CURSOR_NAMESPACE,
    GitHubReconciler,
    InvalidPollPageError,
)
from ultimate_coders.trigger.github_webhook import (
    ParsedEvent,
    canonical_payload_json,
    ingest_payload,
    parse_webhook,
)

REPO = "acme/widgets"
OTHER_REPO = "acme/other"
PR_NUMBER = 12
HEAD_SHA = "a" * 40
BEFORE_SHA = "b" * 40
CHECK_RUN_ID = 424242
COMMENT_ID = 5555


# ── The gateway's dedup rule, re-implemented locally ────────────────────


def dedup_key_of(source: str, kind: str, external_id: str, payload: dict[str, Any]) -> str:
    """``sha256(source|kind|external_id|payload_digest)`` -- as uc-types does it."""
    payload_digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    material = f"{source}|{kind}|{external_id}|{payload_digest}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def dedup_key(event: ParsedEvent) -> str:
    return dedup_key_of(event.source, event.kind, event.external_id, event.payload)


def forwarded_dedup_key(forwarded: dict[str, Any]) -> str:
    """The same rule applied to what we actually forward to ``trigger.ingest``."""
    return dedup_key_of(
        forwarded["source"],
        forwarded["kind"],
        forwarded["external_id"],
        json.loads(forwarded["payload_json"]),
    )


# ── Test doubles ───────────────────────────────────────────────────────


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeTransport:
    """The injected fetch: records what the reconciler asked for."""

    def __init__(
        self,
        pages: dict[str, Any] | None = None,
        failing: tuple = (),
    ) -> None:
        self.pages = pages or {}
        self.failing = set(failing)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        repo: str,
        *,
        etag: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"repo": repo, "etag": etag, "cursor": cursor})
        if repo in self.failing:
            raise RuntimeError(f"transport down for {repo}")
        return self.pages[repo]


def _state(tmp_path) -> RuntimeState:
    return RuntimeState(path=tmp_path / "reconciler-state.sqlite3")


def _page(*entries: dict[str, Any], etag: str = "etag-1") -> dict[str, Any]:
    return {"items": list(entries), "etag": etag}


def _check_run_entry(
    event_id: str,
    *,
    run_id: int = CHECK_RUN_ID,
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "CheckRunEvent",
        "actor": {"login": "octocat"},
        "repo": {"name": REPO},
        "payload": {
            "action": "completed",
            "check_run": {
                "id": run_id,
                "status": status,
                "conclusion": conclusion,
                "head_sha": HEAD_SHA,
                "pull_requests": [{"number": PR_NUMBER}],
            },
        },
    }


# ── Convergence with the webhook twin ──────────────────────────────────

CONVERGENCE_CASES: dict[str, tuple] = {
    "check_run": (
        "check_run",
        "CheckRunEvent",
        {
            "action": "completed",
            "check_run": {
                "id": CHECK_RUN_ID,
                "status": "completed",
                "conclusion": "success",
                "head_sha": HEAD_SHA,
                "pull_requests": [{"number": PR_NUMBER}],
            },
        },
    ),
    "issue_comment": (
        "issue_comment",
        "IssueCommentEvent",
        {
            "action": "created",
            "issue": {"id": 9001, "number": PR_NUMBER, "pull_request": {}},
            "comment": {"id": COMMENT_ID, "body": "please take a look"},
        },
    ),
    "push": (
        "push",
        "PushEvent",
        {
            "ref": "refs/heads/main",
            "before": BEFORE_SHA,
            "head": HEAD_SHA,
            "size": 1,
            "commits": [{"id": HEAD_SHA}],
        },
    ),
}


def _webhook_delivery(case: str) -> tuple:
    event_name, _entry_type, payload = CONVERGENCE_CASES[case]
    body = dict(payload)
    if event_name == "push":
        # Webhook bodies call the head commit ``after``; the events API says
        # ``head``. Same commit, and identity must not care.
        body["after"] = body.pop("head")
    body["repository"] = {"full_name": REPO}
    body["sender"] = {"login": "octocat"}
    return event_name, body


def _poll_entry(case: str, event_id: str) -> dict[str, Any]:
    _event_name, entry_type, payload = CONVERGENCE_CASES[case]
    return {
        "id": event_id,
        "type": entry_type,
        "actor": {"login": "octocat"},
        "repo": {"name": REPO},
        "payload": payload,
    }


@pytest.mark.parametrize("case", sorted(CONVERGENCE_CASES))
async def test_polled_event_and_its_webhook_twin_share_the_dedup_key(
    case: str, tmp_path
) -> None:
    event_name, body = _webhook_delivery(case)
    webhook_event = parse_webhook(
        {"X-GitHub-Event": event_name, "X-GitHub-Delivery": "delivery-1"},
        json.dumps(body).encode("utf-8"),
    )
    assert webhook_event is not None

    transport = FakeTransport({REPO: _page(_poll_entry(case, "41"))})
    reconciler = GitHubReconciler(
        transport, [REPO], runtime_state=_state(tmp_path), clock=FakeClock()
    )
    polled = await reconciler.poll_once(REPO)

    assert len(polled) == 1
    event = polled[0]
    assert (event.source, event.kind, event.external_id, event.thread_key) == (
        webhook_event.source,
        webhook_event.kind,
        webhook_event.external_id,
        webhook_event.thread_key,
    )
    assert event.payload == webhook_event.payload
    assert canonical_payload_json(event.payload) == canonical_payload_json(
        webhook_event.payload
    )
    assert dedup_key(event) == dedup_key(webhook_event)
    # Stronger: the actual ingest requests converge, not just the parsed events.
    polled_forwarded = ingest_payload(event)
    webhook_forwarded = ingest_payload(webhook_event)
    assert polled_forwarded["external_id"] == webhook_forwarded["external_id"]
    assert polled_forwarded["payload_json"] == webhook_forwarded["payload_json"]
    assert forwarded_dedup_key(polled_forwarded) == forwarded_dedup_key(webhook_forwarded)
    # A delivery id belongs to the webhook transport only; identity excludes it.
    assert event.delivery_id is None
    assert webhook_event.delivery_id == "delivery-1"


# ── Cursor behaviour ───────────────────────────────────────────────────


async def test_poll_emits_oldest_first_and_advances_the_cursor(tmp_path) -> None:
    transport = FakeTransport(
        {REPO: _page(_check_run_entry("3", run_id=3), _check_run_entry("2", run_id=2),
                     _check_run_entry("1", run_id=1))}
    )
    reconciler = GitHubReconciler(
        transport, [REPO], runtime_state=_state(tmp_path), clock=FakeClock()
    )

    events = await reconciler.poll_once(REPO)
    assert [event.external_id for event in events] == [
        "check_run:1:completed:success",
        "check_run:2:completed:success",
        "check_run:3:completed:success",
    ]
    record = reconciler.cursor(REPO)
    assert record["last_event_id"] == "3"
    assert record["etag"] == "etag-1"
    assert record["failures"] == 0
    assert record["last_polled_at"] == 1000.0

    assert await reconciler.poll_once(REPO) == []
    assert transport.calls[-1] == {"repo": REPO, "etag": "etag-1", "cursor": "3"}


async def test_cursor_survives_a_new_reconciler_instance(tmp_path) -> None:
    state = _state(tmp_path)
    first_transport = FakeTransport({REPO: _page(_check_run_entry("7"))})
    first = GitHubReconciler(first_transport, [REPO], runtime_state=state, clock=FakeClock())
    assert len(await first.poll_once(REPO)) == 1

    second_transport = FakeTransport({REPO: _page(_check_run_entry("7"))})
    second = GitHubReconciler(second_transport, [REPO], runtime_state=state, clock=FakeClock())
    assert await second.poll_once(REPO) == []
    assert second_transport.calls[0]["cursor"] == "7"
    assert second.cursor(REPO)["last_event_id"] == "7"


async def test_max_items_bounds_each_poll_and_drains_oldest_first(tmp_path) -> None:
    entries = [_check_run_entry(str(index), run_id=index) for index in range(1, 6)]
    transport = FakeTransport({REPO: _page(*reversed(entries))})
    reconciler = GitHubReconciler(
        transport, [REPO], runtime_state=_state(tmp_path), clock=FakeClock(), max_items=2
    )

    first = await reconciler.poll_once(REPO)
    second = await reconciler.poll_once(REPO)
    third = await reconciler.poll_once(REPO)
    fourth = await reconciler.poll_once(REPO)

    assert [event.external_id for event in first] == [
        "check_run:1:completed:success",
        "check_run:2:completed:success",
    ]
    assert [event.external_id for event in second] == [
        "check_run:3:completed:success",
        "check_run:4:completed:success",
    ]
    assert [event.external_id for event in third] == ["check_run:5:completed:success"]
    assert fourth == []
    assert reconciler.cursor(REPO)["last_event_id"] == "5"


async def test_unsupported_event_types_are_skipped_but_the_cursor_advances(tmp_path) -> None:
    transport = FakeTransport(
        {
            REPO: _page(
                {"id": "2", "type": "WatchEvent", "actor": {}, "repo": {"name": REPO}},
                _check_run_entry("1"),
            )
        }
    )
    reconciler = GitHubReconciler(
        transport, [REPO], runtime_state=_state(tmp_path), clock=FakeClock()
    )
    events = await reconciler.poll_once(REPO)
    assert [event.external_id for event in events] == ["check_run:424242:completed:success"]
    # The skipped type still moved the cursor: re-reading it every poll would
    # starve the events behind it.
    assert reconciler.cursor(REPO)["last_event_id"] == "2"


async def test_malformed_supported_entry_is_counted_and_does_not_kill_the_poll(tmp_path) -> None:
    transport = FakeTransport(
        {
            REPO: _page(
                {"id": "2", "type": "CheckRunEvent", "actor": {}, "repo": {"name": REPO},
                 "payload": {"action": "completed"}},
                _check_run_entry("1"),
            )
        }
    )
    reconciler = GitHubReconciler(
        transport, [REPO], runtime_state=_state(tmp_path), clock=FakeClock()
    )
    events = await reconciler.poll_once(REPO)
    assert [event.external_id for event in events] == ["check_run:424242:completed:success"]
    record = reconciler.cursor(REPO)
    assert record["last_skipped"] == 1
    assert record["last_event_id"] == "2"


async def test_duplicate_event_ids_in_one_page_are_processed_once(tmp_path) -> None:
    transport = FakeTransport(
        {REPO: _page(_check_run_entry("1", run_id=1), _check_run_entry("1", run_id=2))}
    )
    reconciler = GitHubReconciler(
        transport, [REPO], runtime_state=_state(tmp_path), clock=FakeClock()
    )
    events = await reconciler.poll_once(REPO)
    assert [event.external_id for event in events] == ["check_run:1:completed:success"]


async def test_cursors_are_isolated_per_repository(tmp_path) -> None:
    transport = FakeTransport(
        {
            REPO: _page(_check_run_entry("10")),
            OTHER_REPO: _page(_check_run_entry("20")),
        }
    )
    reconciler = GitHubReconciler(
        transport, [REPO, OTHER_REPO], runtime_state=_state(tmp_path), clock=FakeClock()
    )
    events = await reconciler.poll()
    assert len(events) == 2
    assert reconciler.cursor(REPO)["last_event_id"] == "10"
    assert reconciler.cursor(OTHER_REPO)["last_event_id"] == "20"
    assert {call["repo"] for call in transport.calls} == {REPO, OTHER_REPO}


# ── Bounded failure behaviour ──────────────────────────────────────────


async def test_fetch_failure_records_exponential_backoff(tmp_path) -> None:
    clock = FakeClock(1000.0)
    transport = FakeTransport(failing=(REPO,))
    reconciler = GitHubReconciler(
        transport,
        [REPO],
        runtime_state=_state(tmp_path),
        backoff_base_seconds=1.0,
        backoff_max_seconds=8.0,
        clock=clock,
    )

    with pytest.raises(RuntimeError):
        await reconciler.poll_once(REPO)
    record = reconciler.cursor(REPO)
    assert record["failures"] == 1
    assert record["next_poll_at"] == 1001.0
    assert "RuntimeError: transport down" in record["last_error"]
    assert len(transport.calls) == 1

    # Still backing off: no transport call, no events.
    clock.now = 1000.5
    assert await reconciler.poll_once(REPO) == []
    assert len(transport.calls) == 1

    delays = [1.0]
    for expected_delay in (2.0, 4.0, 8.0, 8.0):
        clock.now = record["next_poll_at"]
        with pytest.raises(RuntimeError):
            await reconciler.poll_once(REPO)
        record = reconciler.cursor(REPO)
        assert record["next_poll_at"] == clock.now + expected_delay
        delays.append(expected_delay)

    assert record["failures"] == 5
    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]


async def test_success_after_failure_resets_the_backoff(tmp_path) -> None:
    clock = FakeClock(1000.0)
    transport = FakeTransport({REPO: _page(_check_run_entry("1"))}, failing=(REPO,))
    reconciler = GitHubReconciler(
        transport,
        [REPO],
        runtime_state=_state(tmp_path),
        backoff_base_seconds=1.0,
        clock=clock,
    )
    with pytest.raises(RuntimeError):
        await reconciler.poll_once(REPO)
    assert reconciler.cursor(REPO)["failures"] == 1

    transport.failing.clear()
    clock.now = reconciler.cursor(REPO)["next_poll_at"]
    events = await reconciler.poll_once(REPO)
    assert [event.external_id for event in events] == ["check_run:424242:completed:success"]
    record = reconciler.cursor(REPO)
    assert record["failures"] == 0
    assert record["next_poll_at"] == 0.0
    assert record["last_error"] == ""


async def test_one_failing_repository_does_not_stall_the_others(tmp_path) -> None:
    transport = FakeTransport(
        {REPO: _page(_check_run_entry("5"))}, failing=(OTHER_REPO,)
    )
    reconciler = GitHubReconciler(
        transport, [OTHER_REPO, REPO], runtime_state=_state(tmp_path), clock=FakeClock()
    )
    events = await reconciler.poll()
    assert [event.external_id for event in events] == ["check_run:424242:completed:success"]
    assert reconciler.cursor(OTHER_REPO)["failures"] == 1


async def test_invalid_page_is_a_typed_error_and_records_backoff(tmp_path) -> None:
    transport = FakeTransport({REPO: {"items": "not-a-list"}})
    reconciler = GitHubReconciler(
        transport, [REPO], runtime_state=_state(tmp_path), clock=FakeClock()
    )
    with pytest.raises(InvalidPollPageError):
        await reconciler.poll_once(REPO)
    record = reconciler.cursor(REPO)
    assert record["failures"] == 1
    assert "InvalidPollPageError" in record["last_error"]


# ── Construction guards ────────────────────────────────────────────────


def test_reconciler_rejects_unusable_configuration(tmp_path) -> None:
    transport = FakeTransport({})
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        GitHubReconciler(transport, [], runtime_state=state)
    with pytest.raises(ValueError):
        GitHubReconciler(transport, ["no-slash"], runtime_state=state)
    with pytest.raises(ValueError):
        GitHubReconciler(transport, [REPO], runtime_state=state, max_items=0)
    with pytest.raises(ValueError):
        GitHubReconciler(
            transport,
            [REPO],
            runtime_state=state,
            backoff_base_seconds=5.0,
            backoff_max_seconds=1.0,
        )
    with pytest.raises(TypeError):
        GitHubReconciler(transport, REPO, runtime_state=state)


async def test_cursor_namespace_is_the_durable_one(tmp_path) -> None:
    state = _state(tmp_path)
    transport = FakeTransport({REPO: _page(_check_run_entry("1"))})
    reconciler = GitHubReconciler(transport, [REPO], runtime_state=state, clock=FakeClock())
    assert reconciler.cursor(REPO) == {}

    await reconciler.poll_once(REPO)
    assert state.get(CURSOR_NAMESPACE, REPO)["last_event_id"] == "1"
