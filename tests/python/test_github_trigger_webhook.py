"""PR04 -- GitHub webhook delivery -> canonical trigger identity.

The invariant under test: one GitHub event has exactly one canonical identity
(``kind``, ``external_id``, ``thread_key``, canonical payload), because the
gateway derives its dedup key from those fields. A comment, a CI result and a
status change on the same pull request must therefore land on the same thread,
and a re-delivery must reduce to the same event. The payload is a projection,
not the raw body, so the webhook and the polling reconciler can agree on it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
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
    canonical_payload_json,
    ingest_payload,
    is_known_kind,
    is_known_source,
    normalize_thread_key,
    parse_webhook,
    verify_signature,
)

SECRET = "webhook-secret"
REPO = "acme/widgets"
PR_NUMBER = 12
PR_ID = 7777
COMMENT_ID = 5555
CHECK_RUN_ID = 424242
REVIEW_ID = 3131
WORKFLOW_RUN_ID = 8888
HEAD_SHA = "a" * 40
BEFORE_SHA = "b" * 40


# ── Fixtures / helpers ─────────────────────────────────────────────────


def _headers(event: str, delivery: str | None = "delivery-1") -> dict[str, str]:
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return headers


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _parse(
    event: str, payload: dict[str, Any], delivery: str | None = "delivery-1"
) -> ParsedEvent | None:
    body = json.dumps(payload).encode("utf-8")
    return parse_webhook(_headers(event, delivery), body)


def _repository() -> dict[str, Any]:
    return {"full_name": REPO}


def _sender() -> dict[str, Any]:
    return {"login": "octocat"}


def _issue_comment_payload(
    comment_id: int = COMMENT_ID, number: int = PR_NUMBER, pull_request: bool = True
) -> dict[str, Any]:
    issue: dict[str, Any] = {"id": 9001, "number": number}
    if pull_request:
        issue["pull_request"] = {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}
    return {
        "action": "created",
        "issue": issue,
        "comment": {"id": comment_id, "body": "please take a look"},
        "repository": _repository(),
        "sender": _sender(),
    }


def _check_run_payload(
    run_id: int = CHECK_RUN_ID,
    number: int = PR_NUMBER,
    status: str = "completed",
    conclusion: str | None = "success",
    head_sha: str = HEAD_SHA,
    pull_requests: list | None = None,
) -> dict[str, Any]:
    if pull_requests is None:
        pull_requests = [{"number": number}]
    return {
        "action": "completed",
        "check_run": {
            "id": run_id,
            "status": status,
            "conclusion": conclusion,
            "head_sha": head_sha,
            "pull_requests": pull_requests,
            "output": {"title": "volatile detail that must not enter the payload"},
        },
        "repository": _repository(),
        "sender": _sender(),
    }


def _workflow_run_payload(
    run_id: int = WORKFLOW_RUN_ID,
    number: int = PR_NUMBER,
    head_sha: str = HEAD_SHA,
) -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "status": "completed",
            "conclusion": "success",
            "head_sha": head_sha,
            "pull_requests": [{"number": number}],
        },
        "repository": _repository(),
        "sender": _sender(),
    }


def _pull_request_payload(
    action: str = "synchronize",
    number: int = PR_NUMBER,
    pull_id: int = PR_ID,
    head_sha: str = HEAD_SHA,
) -> dict[str, Any]:
    return {
        "action": action,
        "number": number,
        "pull_request": {"id": pull_id, "number": number, "head": {"sha": head_sha}},
        "repository": _repository(),
        "sender": _sender(),
    }


def _push_payload(
    ref: str = "refs/heads/main", after: str = HEAD_SHA, before: str = BEFORE_SHA
) -> dict[str, Any]:
    return {
        "ref": ref,
        "before": before,
        "after": after,
        "repository": _repository(),
        "sender": _sender(),
    }


# ── Signature verification ─────────────────────────────────────────────


def test_verify_signature_accepts_github_sha256_header() -> None:
    body = b'{"action": "created"}'
    assert verify_signature(SECRET, body, _sign(body)) is True


def test_verify_signature_rejects_tampered_body() -> None:
    body = b'{"action": "created"}'
    assert verify_signature(SECRET, body + b" ", _sign(body)) is False


def test_verify_signature_rejects_wrong_secret() -> None:
    body = b'{"action": "created"}'
    assert verify_signature(SECRET, body, _sign(body, secret="other")) is False


@pytest.mark.parametrize(
    "header",
    [None, "", "sha1=deadbeef", "sha256=", "sha256=not-a-digest", "deadbeef"],
)
def test_verify_signature_refuses_missing_or_foreign_headers(header: str | None) -> None:
    body = b"{}"
    assert verify_signature(SECRET, body, header) is False


def test_verify_signature_refuses_empty_secret() -> None:
    body = b"{}"
    assert verify_signature("", body, _sign(body)) is False


def test_verify_signature_tolerates_a_non_ascii_header() -> None:
    # hmac.compare_digest(str, str) raises on non-ASCII; the bytes comparison
    # must return False instead of letting a 500 escape the API.
    assert verify_signature(SECRET, b"{}", "sha256=\u4f60\u597d") is False


def test_verify_signature_is_case_insensitive_on_the_hex_digest() -> None:
    body = b'{"a": 1}'
    digest = _sign(body)[len("sha256=") :]
    assert verify_signature(SECRET, body, "sha256=" + digest.upper()) is True


# ── Thread identity ────────────────────────────────────────────────────


def test_normalize_thread_key_is_stable() -> None:
    assert normalize_thread_key(REPO, KIND_PULL_REQUEST_COMMENT, 12) == "acme/widgets#12"


def test_normalize_thread_key_rejects_kinds_without_a_numbered_thread() -> None:
    with pytest.raises(ValueError):
        normalize_thread_key(REPO, KIND_PUSH, 12)


@pytest.mark.parametrize("repo, number", [("", 12), ("widgets", 12), (REPO, 0), (REPO, -1)])
def test_normalize_thread_key_rejects_unusable_input(repo: str, number: int) -> None:
    with pytest.raises(ValueError):
        normalize_thread_key(repo, KIND_ISSUE_OPENED, number)


def test_comment_ci_result_and_status_change_share_one_thread() -> None:
    """Acceptance: three different events on PR #12 are one thread."""
    comment = _parse("issue_comment", _issue_comment_payload())
    ci = _parse("check_run", _check_run_payload())
    status = _parse("pull_request", _pull_request_payload("synchronize"))
    workflow = _parse("workflow_run", _workflow_run_payload())

    events = [event for event in (comment, ci, status, workflow) if event is not None]
    assert len(events) == 4
    assert {event.thread_key for event in events} == {"acme/widgets#12"}
    assert {event.repo for event in events} == {REPO}
    assert {event.kind for event in events} == {
        KIND_PULL_REQUEST_COMMENT,
        KIND_CHECK_RUN,
        KIND_PULL_REQUEST_SYNCHRONIZE,
        KIND_WORKFLOW_RUN,
    }
    # Same thread, different events: identity must not collapse them.
    assert len({event.external_id for event in events}) == 4


def test_check_run_without_a_linked_pull_request_uses_the_commit_thread() -> None:
    event = _parse("check_run", _check_run_payload(pull_requests=[]))
    assert event is not None
    assert event.thread_key == f"{REPO}@{HEAD_SHA}"


def test_push_maps_to_the_commit_thread() -> None:
    event = _parse("push", _push_payload())
    assert event is not None
    assert event.kind == KIND_PUSH
    assert event.thread_key == f"{REPO}@{HEAD_SHA}"
    assert event.external_id == f"push:refs/heads/main:{HEAD_SHA}"


def test_push_to_a_deleted_branch_has_no_commit_thread() -> None:
    payload = _push_payload(after="0" * 40)
    payload["deleted"] = True
    event = _parse("push", payload)
    assert event is not None
    # The all-zero sha is not a commit; threading every deletion on it would
    # collapse unrelated branches onto one thread.
    assert event.thread_key is None
    assert event.external_id == f"push:refs/heads/main:{'0' * 40}"


def test_push_accepts_the_events_api_head_spelling() -> None:
    """The events API names the head commit ``head``; identity must not care."""
    payload = _push_payload()
    payload["head"] = payload.pop("after")
    event = _parse("push", payload)
    assert event is not None
    assert event.thread_key == f"{REPO}@{HEAD_SHA}"
    assert event.external_id == f"push:refs/heads/main:{HEAD_SHA}"


# ── Event mapping ──────────────────────────────────────────────────────


def test_issue_comment_on_a_pull_request_is_a_pull_request_comment() -> None:
    event = _parse("issue_comment", _issue_comment_payload())
    assert event is not None
    assert event.kind == KIND_PULL_REQUEST_COMMENT
    assert event.external_id == f"comment:{COMMENT_ID}"
    assert event.thread_key == f"{REPO}#{PR_NUMBER}"
    assert event.delivery_id == "delivery-1"
    assert event.event_name == "issue_comment"


def test_issue_comment_on_a_plain_issue_is_an_issue_comment() -> None:
    event = _parse("issue_comment", _issue_comment_payload(pull_request=False))
    assert event is not None
    assert event.kind == KIND_ISSUE_COMMENT
    assert event.thread_key == f"{REPO}#{PR_NUMBER}"


def test_issue_opened_maps_to_issue_opened() -> None:
    payload = {
        "action": "opened",
        "issue": {"id": 9001, "number": 34},
        "repository": _repository(),
        "sender": _sender(),
    }
    event = _parse("issues", payload)
    assert event is not None
    assert event.kind == KIND_ISSUE_OPENED
    assert event.external_id == "issue:9001"
    assert event.thread_key == f"{REPO}#34"


def test_pull_request_opened_maps_to_pull_request_opened() -> None:
    event = _parse("pull_request", _pull_request_payload("opened"))
    assert event is not None
    assert event.kind == KIND_PULL_REQUEST_OPENED
    assert event.external_id == f"pull_request:{PR_ID}:opened"
    assert event.thread_key == f"{REPO}#{PR_NUMBER}"


def test_pull_request_synchronize_identity_includes_the_new_head() -> None:
    first = _parse("pull_request", _pull_request_payload("synchronize", head_sha="a" * 40))
    second = _parse("pull_request", _pull_request_payload("synchronize", head_sha="c" * 40))
    assert first is not None and second is not None
    assert first.external_id != second.external_id
    assert first.thread_key == second.thread_key == f"{REPO}#{PR_NUMBER}"


def test_pull_request_review_maps_to_its_own_identity() -> None:
    payload = {
        "action": "submitted",
        "review": {"id": REVIEW_ID, "state": "approved"},
        "pull_request": {"id": PR_ID, "number": PR_NUMBER, "head": {"sha": HEAD_SHA}},
        "repository": _repository(),
        "sender": _sender(),
    }
    event = _parse("pull_request_review", payload)
    assert event is not None
    assert event.kind == KIND_PULL_REQUEST_REVIEW
    assert event.external_id == f"review:{REVIEW_ID}"
    assert event.thread_key == f"{REPO}#{PR_NUMBER}"


@pytest.mark.parametrize(
    "event_name, payload",
    [
        ("issues", {"action": "closed", "issue": {"id": 1, "number": 1}}),
        ("issue_comment", {"action": "edited", "issue": {"id": 1, "number": 1}}),
        ("pull_request", {"action": "closed", "pull_request": {"id": 1, "number": 1}}),
        ("pull_request_review", {"action": "dismissed", "review": {"id": 1}}),
        ("star", {"action": "created"}),
        ("ping", {"zen": "Keep it logically awesome."}),
    ],
)
def test_unmapped_events_and_actions_are_ignored(
    event_name: str, payload: dict[str, Any]
) -> None:
    assert _parse(event_name, payload) is None


@pytest.mark.parametrize(
    "event_name, payload",
    [
        ("check_run", {"action": "completed", "check_run": {"status": "completed"}}),
        ("push", {"ref": "refs/heads/main", "repository": _repository()}),
        (
            "pull_request",
            {"action": "synchronize", "pull_request": {"id": 1, "number": 1}},
        ),
        (
            "issue_comment",
            {"action": "created", "issue": {"number": 1}, "comment": {"body": "x"}},
        ),
        ("issues", {"action": "opened", "issue": {"number": 1}}),
    ],
)
def test_events_without_stable_identity_are_rejected(
    event_name: str, payload: dict[str, Any]
) -> None:
    with pytest.raises(InvalidPayloadError):
        _parse(event_name, payload)


# ── Payload projection and purity ──────────────────────────────────────


def test_payload_is_a_projection_of_identity_and_status_only() -> None:
    event = _parse("check_run", _check_run_payload())
    assert event is not None
    assert event.payload == {
        "action": "completed",
        "check_run_id": CHECK_RUN_ID,
        "status": "completed",
        "conclusion": "success",
        "number": PR_NUMBER,
        "sha": HEAD_SHA,
    }
    # Raw wrapper and volatile detail must not leak into the digested payload.
    assert "repository" not in event.payload
    assert "sender" not in event.payload
    assert "output" not in event.payload


def test_parse_is_pure_and_redelivery_keeps_the_identity() -> None:
    payload = _issue_comment_payload()
    first = _parse("issue_comment", payload, delivery="delivery-1")
    again = _parse("issue_comment", payload, delivery="delivery-1")
    redelivered = _parse("issue_comment", payload, delivery="delivery-2")
    assert first == again
    assert first is not None and redelivered is not None
    assert first.external_id == redelivered.external_id
    assert first.thread_key == redelivered.thread_key
    assert first.payload == redelivered.payload
    # The delivery id is transport metadata: it is forwarded, never identity.
    assert first.delivery_id != redelivered.delivery_id


def test_ingest_payload_carries_the_canonical_identity() -> None:
    event = _parse("issue_comment", _issue_comment_payload())
    assert event is not None
    forwarded = ingest_payload(event)
    assert forwarded["source"] == "github"
    assert forwarded["kind"] == KIND_PULL_REQUEST_COMMENT
    assert forwarded["external_id"] == f"comment:{COMMENT_ID}"
    assert forwarded["thread_key"] == f"{REPO}#{PR_NUMBER}"
    assert forwarded["delivery_id"] == "delivery-1"
    assert json.loads(forwarded["payload_json"]) == event.payload
    # The caller is the authenticated service; the event itself gets no authority.
    assert forwarded["principal_kind"] == "service"
    assert forwarded["open_case_if_unmatched"] is False


def test_canonical_payload_json_is_deterministic_and_unicode_safe() -> None:
    left = {"b": 1, "a": "\u4f60\u597d"}
    right = {"a": "\u4f60\u597d", "b": 1}
    assert canonical_payload_json(left) == canonical_payload_json(right)
    assert canonical_payload_json(left) == '{"a":"\u4f60\u597d","b":1}'


def test_header_lookup_is_case_insensitive() -> None:
    body = json.dumps(_push_payload()).encode("utf-8")
    headers = {"x-github-event": "push", "x-github-delivery": "delivery-9"}
    event = parse_webhook(headers, body)
    assert event is not None
    assert event.kind == KIND_PUSH
    assert event.delivery_id == "delivery-9"


def test_missing_event_header_is_rejected() -> None:
    with pytest.raises(InvalidPayloadError):
        parse_webhook({"X-GitHub-Delivery": "delivery-1"}, b"{}")


def test_oversized_body_is_rejected_before_json_parsing() -> None:
    # Deliberately not JSON: the size check must run first.
    body = b"{" + b" " * MAX_PAYLOAD_BYTES
    with pytest.raises(PayloadTooLargeError):
        parse_webhook(_headers("push"), body)


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe", b"[]", b'"scalar"', b"null"])
def test_non_object_bodies_are_rejected(body: bytes) -> None:
    with pytest.raises(InvalidPayloadError):
        parse_webhook(_headers("push"), body)


# ── Vocabulary ─────────────────────────────────────────────────────────


def test_kind_and_source_vocabulary_is_the_contract() -> None:
    assert KIND_TOKENS == {
        "issue_opened",
        "issue_comment",
        "pull_request_opened",
        "pull_request_synchronize",
        "pull_request_review",
        "pull_request_comment",
        "push",
        "check_run",
        "workflow_run",
    }
    assert SOURCE_TOKENS == {"github", "ci", "lark", "dashboard", "api", "cron", "manual"}
    assert SOURCE_GITHUB == "github"
    assert is_known_kind("check_run") is True
    assert is_known_kind(" Check_Run ") is True
    assert is_known_kind("starred") is False
    assert is_known_kind(None) is False
    assert is_known_source("github") is True
    assert is_known_source("") is False
