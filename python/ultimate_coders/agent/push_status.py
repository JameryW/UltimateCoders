"""The single vocabulary of workspace/arbiter ``push_status`` verdicts.

Two modules publish a branch (``agent/workspace.py`` for a subtask branch,
``agent/merge_arbiter.py`` for the base branch) and several consume the verdict.
T68 was one of these drift bugs: T56/T57 added ``"unknown"`` and ``"busy"`` to the
producers, and the worker's delivery gate — written earlier — only knew
``"failed"``, so an unconfirmed publish was reported as a completed subtask.

Keeping the vocabulary in one place makes that impossible to repeat silently:

- ``PUSH_STATUSES`` is the complete set a producer may write;
- ``tests/python/test_push_status_vocabulary.py`` scans the producers' source for
  literal assignments and fails when one is not in this list;
- the same test walks ``PUSH_STATUSES`` through the worker's delivery gate, so a
  verdict with no consumer decision cannot be added either.
"""

from __future__ import annotations

from typing import Literal

#: Every verdict a producer may write into ``result_info["push_status"]``.
PushStatus = Literal["pushed", "already_pushed", "failed", "unknown", "busy", "skipped"]

PUSH_STATUSES: tuple[PushStatus, ...] = (
    "pushed",
    "already_pushed",
    "failed",
    "unknown",
    "busy",
    "skipped",
)

#: The publish is known to be on the remote.
APPLIED_PUSH_STATUSES: frozenset[str] = frozenset({"pushed", "already_pushed"})

#: The publish is not (provably) on the remote. ``skipped`` is not here: it means
#: "no publish was attempted" (no remote configured), which is a deliberate state,
#: not an unconfirmed delivery.
UNCONFIRMED_PUSH_STATUSES: frozenset[str] = frozenset({"failed", "unknown", "busy"})

#: Subset of the unconfirmed verdicts that a retry can still settle: the claim is
#: held elsewhere (``busy``) or the publish awaits reconciliation (``unknown``).
#: ``failed`` is definite — retrying reproduces it.
TRANSIENT_PUSH_STATUSES: frozenset[str] = frozenset({"unknown", "busy"})
