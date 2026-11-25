"""Read-side half of supersede-not-delete (T11 follow-up, proposal 29).

Writes keep marking rather than deleting, so a recall page can contain both an
old fact and the newer fact that replaced it. The read path must drop the old
entry — otherwise the model sees conflicting conventions and cannot tell which
one the repository follows today. The stored history stays intact.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from ultimate_coders.agent.hy_memory import (  # noqa: E402
    SUPERSEDED_REMOVED_KEY,
    SUPERSEDES_METADATA_KEY,
    CodingMemory,
    filter_superseded_facts,
)


class RecallClient:
    """Fake client whose search returns a page containing old and new facts."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.search_calls: list[dict[str, Any]] = []

    def search(self, _query: str, **kwargs: Any) -> dict[str, Any]:
        self.search_calls.append(kwargs)
        return dict(self.payload)

    def close(self) -> None:  # pragma: no cover - wrapper-only path
        return None


def _page() -> dict[str, Any]:
    return {
        "facts": [
            {
                "id": "f-old",
                "statement": "Money amounts are floats rounded half-up.",
            },
            {
                "id": "f-new",
                "statement": "Money amounts are integer cents rounded down.",
                "metadata": {SUPERSEDES_METADATA_KEY: ["f-old"]},
            },
            {"id": "f-unrelated", "statement": "The config port is 8080."},
        ],
        "experiences": [{"task": "pricing", "solution": "integer cents"}],
        "chain_block": ["pricing -> cents"],
    }


def test_filter_removes_only_superseded_facts() -> None:
    kept, removed = filter_superseded_facts(_page()["facts"])

    assert [fact["id"] for fact in kept] == ["f-new", "f-unrelated"]
    assert removed == ["f-old"]


def test_filter_preserves_identity_without_supersede_metadata() -> None:
    facts = [{"id": "a"}, {"id": "b"}]

    kept, removed = filter_superseded_facts(facts)

    assert kept == facts
    assert removed == []


def test_filter_tolerates_non_fact_entries_and_bare_payloads() -> None:
    kept, removed = filter_superseded_facts(["plain text", None, {"id": "x"}])

    assert kept == ["plain text", None, {"id": "x"}]
    assert removed == []


def test_recall_reports_removed_ids_and_keeps_raw_page() -> None:
    page = _page()
    client = RecallClient(page)
    mem = CodingMemory(user_id="u", client=client)

    payload = mem.recall("pricing convention")

    assert payload["ok"] is True
    assert [fact["id"] for fact in payload["facts"]] == ["f-new", "f-unrelated"]
    assert payload[SUPERSEDED_REMOVED_KEY] == ["f-old"]
    # The full backend page is still exposed for callers that want the raw
    # history; only the injected facts are filtered.
    assert [fact["id"] for fact in payload["raw"]["facts"]] == [
        "f-old",
        "f-new",
        "f-unrelated",
    ]


def test_recall_without_supersede_metadata_has_no_removed_key() -> None:
    client = RecallClient({"facts": [{"id": "a", "statement": "only fact"}]})
    mem = CodingMemory(user_id="u", client=client)

    payload = mem.recall("anything")

    assert payload[SUPERSEDED_REMOVED_KEY] if SUPERSEDED_REMOVED_KEY in payload else True
    assert SUPERSEDED_REMOVED_KEY not in payload


def test_recall_keeps_failed_search_contract() -> None:
    class BrokenClient:
        def search(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("endpoint down")

    mem = CodingMemory(user_id="u", client=BrokenClient())

    payload = mem.recall("anything")

    assert payload["ok"] is False
    assert payload["facts"] == []
    assert SUPERSEDED_REMOVED_KEY not in payload
