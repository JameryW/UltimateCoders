"""Tests for the v10 AgentRadio module (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.radio import (
    KIND_MENTION,
    KIND_THREAD,
    KIND_WORKLOG,
    RADIO_ENABLED_ENV,
    RADIO_INBOX_DIR_ENV,
    BackgroundMentionWatcher,
    InMemoryRadioBus,
    RadioFileInbox,
    RadioMessage,
    mention_subject,
    radio_enabled,
    worklog_subject,
)


def _watcher(
    bus: InMemoryRadioBus,
    *,
    graph_id: str = "graph-1",
    node_id: str = "node-a",
    max_pending: int = 20,
    max_prompt_chars: int = 4000,
) -> BackgroundMentionWatcher:
    counter = iter(range(1000))

    def next_id() -> str:
        return f"msg-{next(counter)}"

    return BackgroundMentionWatcher(
        bus,
        graph_id=graph_id,
        node_id=node_id,
        max_pending=max_pending,
        max_prompt_chars=max_prompt_chars,
        id_factory=next_id,
        clock=lambda: 123.5,
    )


def test_topic_format() -> None:
    assert worklog_subject("graph-1") == "uc.radio.graph-1.worklog"
    assert mention_subject("graph-1", "node-a") == "uc.radio.graph-1.mention.node-a"


def test_topic_validation_rejects_wildcards() -> None:
    with pytest.raises(ValueError):
        worklog_subject("")
    with pytest.raises(ValueError):
        mention_subject("graph-1", "bad>node")


def test_radio_message_round_trip() -> None:
    message = RadioMessage(
        message_id="m1",
        graph_id="g",
        node_id="n",
        kind=KIND_MENTION,
        text="please review",
        created_at=10.0,
        metadata={"priority": 2},
    )
    data = message.to_dict()
    assert data["kind"] == KIND_MENTION
    assert RadioMessage.from_dict(data) == message


def test_radio_message_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        RadioMessage(
            message_id="m1",
            graph_id="g",
            node_id="n",
            kind="shout",
            text="hi",
            created_at=1.0,
        )


def test_post_publishes_worklog() -> None:
    bus = InMemoryRadioBus()
    watcher = _watcher(bus)
    message = watcher.post("compiling the module")
    assert message.kind == KIND_WORKLOG
    assert message.created_at == 123.5
    history = bus.history(worklog_subject("graph-1"))
    assert history == [message]


def test_mention_and_thread_delivery() -> None:
    bus = InMemoryRadioBus()
    alice = _watcher(bus, node_id="node-a")
    bob = _watcher(bus, node_id="node-b")

    mention = alice.mention("please review", target_node_id="node-b")
    assert mention.kind == KIND_MENTION
    assert bob.pending() == [mention]

    thread = alice.create_thread("done", parent_id=mention.message_id, target_node_id="node-b")
    assert thread.kind == KIND_THREAD
    pending = bob.pending()
    assert [item.message_id for item in pending] == [mention.message_id, thread.message_id]
    assert "re " + mention.message_id[:12] in bob.format_update([thread])


def test_pending_is_bounded_and_drops_oldest() -> None:
    bus = InMemoryRadioBus()
    watcher = _watcher(bus, node_id="node-b", max_pending=3)
    for index in range(5):
        bus.publish(
            RadioMessage(
                message_id=f"direct-{index}",
                graph_id="graph-1",
                node_id="node-b",
                kind=KIND_MENTION,
                text=f"message {index}",
                created_at=float(index),
            )
        )
    pending = watcher.pending()
    assert [item.text for item in pending] == ["message 2", "message 3", "message 4"]
    assert watcher.dropped_count == 2


def test_drain_for_prompt_is_bounded() -> None:
    bus = InMemoryRadioBus()
    watcher = _watcher(bus, node_id="node-b", max_pending=10)
    for index in range(6):
        watcher.mention(f"message {index}", target_node_id="node-b")

    first = watcher.drain_for_prompt(limit=2)
    assert first.count("- [") == 2
    remaining = watcher.pending()
    assert len(remaining) == 4

    second = watcher.drain_for_prompt(limit=20)
    assert second.count("- [") == 4
    assert watcher.pending() == []


def test_drain_respects_character_cap() -> None:
    bus = InMemoryRadioBus()
    watcher = _watcher(bus, node_id="node-b", max_prompt_chars=80)
    watcher.mention("x" * 200, target_node_id="node-b")
    output = watcher.drain_for_prompt()
    assert output
    assert len(output) <= 80
    assert watcher.pending() == []


def test_format_update_renders_prompt_block() -> None:
    bus = InMemoryRadioBus()
    watcher = _watcher(bus, node_id="node-b")
    message = watcher.mention("check\nthe lock", target_node_id="node-b")
    rendered = watcher.format_update([message])
    assert rendered.startswith("Radio updates:")
    assert "node-b" in rendered
    assert "check the lock" in rendered
    assert watcher.format_update([]) == ""


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_radio_flag_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(RADIO_ENABLED_ENV, value)
    assert radio_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "banana"])
def test_radio_flag_falsey(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(RADIO_ENABLED_ENV, value)
    assert radio_enabled() is False


def test_radio_flag_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RADIO_ENABLED_ENV, raising=False)
    assert radio_enabled() is False


def test_file_inbox_round_trips_only_matching_messages(tmp_path) -> None:
    inbox = RadioFileInbox(tmp_path, "graph-1", "node-a")
    message = RadioMessage(
        message_id="m-1",
        graph_id="graph-1",
        node_id="node-a",
        kind=KIND_MENTION,
        text="check the lock",
    )
    other = RadioMessage(
        message_id="m-2",
        graph_id="graph-1",
        node_id="node-b",
        kind=KIND_MENTION,
        text="not mine",
    )
    assert inbox.append(message) is True
    assert inbox.append(other) is False
    assert inbox.path.exists()
    assert [item.message_id for item in inbox.read()] == ["m-1"]


def test_file_inbox_skips_malformed_lines(tmp_path) -> None:
    inbox = RadioFileInbox(tmp_path, "graph-1", "node-a")
    inbox.path.parent.mkdir(parents=True, exist_ok=True)
    inbox.path.write_text(
        'not-json\n{"message_id": "m-1", "graph_id": "graph-1", '
        '"node_id": "node-a", "kind": "mention", "text": "ok"}\n',
        encoding="utf-8",
    )
    assert [item.message_id for item in inbox.read()] == ["m-1"]


def test_file_inbox_env_name_is_stable() -> None:
    assert RADIO_INBOX_DIR_ENV == "UC_RADIO_INBOX_DIR"
