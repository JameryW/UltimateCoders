"""A subtask whose publish is unconfirmed is not a completed subtask (T68).

`WorkspaceManager.release` grew two verdicts after the worker's delivery gate was written:
`push_status="unknown"` (the publish was not reconciled yet) and `push_status="busy"` (another
process holds the effect claim). The gate only knew `"failed"`, so those two fell through as
delivery success: the subtask was recorded complete with `success=True`, and the retry that
would have run reconciliation — the mechanism T56/T57 introduced — never happened. The commit
could therefore never reach the remote, silently.
"""

from __future__ import annotations

import pytest
from ultimate_coders.agent.worker import _delivery_verdict


@pytest.mark.parametrize(
    ("merge_result", "expected"),
    [
        ({"status": "conflict"}, (True, False)),
        ({"status": "commit_failed"}, (True, False)),
        ({"push_status": "failed"}, (True, False)),
        # Transient: the retry is what reconciles and completes the publish.
        ({"status": "merged", "push_status": "unknown"}, (True, True)),
        ({"status": "merged", "push_status": "busy"}, (True, True)),
        ({"status": "no_changes", "push_status": "busy"}, (True, True)),
        # Delivery really did happen.
        ({"status": "merged", "push_status": "pushed"}, (False, False)),
        ({"status": "merged", "push_status": "already_pushed"}, (False, False)),
        ({"status": "no_changes"}, (False, False)),
        ({"status": "not_found"}, (False, False)),
        ({}, (False, False)),
    ],
)
def test_delivery_verdict(merge_result, expected):
    assert _delivery_verdict(merge_result) == expected


def test_ambiguous_verdicts_are_retryable_but_terminal_ones_are_not():
    # A retry of an "unknown"/"busy" publish runs reconciliation first, so it is
    # safe; retrying a conflict would only reproduce it.
    assert _delivery_verdict({"push_status": "unknown"}) == (True, True)
    assert _delivery_verdict({"status": "conflict"}) == (True, False)
