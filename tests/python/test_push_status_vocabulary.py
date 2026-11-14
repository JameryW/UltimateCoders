"""Guard the one ``push_status`` vocabulary against silent drift (T68).

T68 was caused by a status value added on the producer side ("unknown", "busy")
that the consuming delivery gate had never been taught. The gate then reported an
unconfirmed publish as a completed subtask.

These tests make that class impossible to land again:

- a literal written by either producer must be in ``push_status.PUSH_STATUSES``;
- every status in that vocabulary must have an explicit delivery decision, and
  every status must be accounted for as applied, unconfirmed, or deliberately
  skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ultimate_coders.agent.push_status import (
    APPLIED_PUSH_STATUSES,
    PUSH_STATUSES,
    TRANSIENT_PUSH_STATUSES,
    UNCONFIRMED_PUSH_STATUSES,
)
from ultimate_coders.agent.worker import _delivery_verdict

_REPO_ROOT = Path(__file__).resolve().parents[2]
#: Every module that writes ``result["push_status"]`` / ``result_info["push_status"]``.
_PRODUCERS = (
    _REPO_ROOT / "python" / "ultimate_coders" / "agent" / "workspace.py",
    _REPO_ROOT / "python" / "ultimate_coders" / "agent" / "merge_arbiter.py",
)
_ASSIGNMENT = re.compile(r'push_status"\]\s*=\s*"([a-z_]+)"')

#: The delivery gate's decision for every possible verdict, written out so that a
#: new vocabulary member has to be classified deliberately.
_EXPECTED_VERDICTS: dict[str, tuple[bool, bool]] = {
    "pushed": (False, False),
    "already_pushed": (False, False),
    "skipped": (False, False),
    "failed": (True, False),
    "unknown": (True, True),
    "busy": (True, True),
}


def _producer_literals() -> set[str]:
    found: set[str] = set()
    for path in _PRODUCERS:
        found.update(_ASSIGNMENT.findall(path.read_text(encoding="utf-8")))
    return found


def test_the_scan_actually_reads_the_producers() -> None:
    """Negative control: if the regex or paths broke, the guard would pass vacuously."""
    literals = _producer_literals()
    assert len(literals) >= 5, f"only found {sorted(literals)}; the scan is broken"
    assert "unknown" in literals
    assert "skipped" in literals


def test_every_producer_literal_is_in_the_vocabulary() -> None:
    unknown = _producer_literals() - set(PUSH_STATUSES)
    assert not unknown, (
        f"producer(s) write push_status {sorted(unknown)} which agent/push_status.py "
        "does not declare -- add it there and teach the delivery gate about it"
    )


def test_every_vocabulary_member_is_classified() -> None:
    assert set(_EXPECTED_VERDICTS) == set(PUSH_STATUSES)


@pytest.mark.parametrize("status", PUSH_STATUSES)
def test_the_delivery_gate_matches_the_declared_verdict(status: str) -> None:
    assert _delivery_verdict({"push_status": status}) == _EXPECTED_VERDICTS[status]


def test_every_status_is_applied_unconfirmed_or_deliberately_skipped() -> None:
    covered = APPLIED_PUSH_STATUSES | UNCONFIRMED_PUSH_STATUSES | {"skipped"}
    assert set(PUSH_STATUSES) <= covered
    assert not (APPLIED_PUSH_STATUSES & UNCONFIRMED_PUSH_STATUSES)
    assert TRANSIENT_PUSH_STATUSES <= UNCONFIRMED_PUSH_STATUSES
