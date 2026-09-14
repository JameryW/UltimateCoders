"""T4 #640 — real JetStream dedup contract for subtask dispatch.

Proves the guarantee the dispatch path now leans on: a message published with
a ``Nats-Msg-Id`` is stored ONCE per ``duplicate_window``. That is what makes
re-dispatching the same ``(graph, node, attempt)`` — a retry, a re-publish, a
gateway restart — harmless: the worker cannot be handed the same attempt
twice.

Scope note: the *consumer*-side scenarios (redelivery caps, ack-after-exec)
are covered by ``test_nats_jetstream_subtask.py`` with mocked JetStream
messages. What cannot be faked is the broker's own duplicate detection, so
that is what this file runs against a live server.

These run against a real NATS with JetStream enabled. When the server is
unreachable they skip LOUDLY rather than passing silently — a silent green
here would certify a contract nothing verified.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

nats = pytest.importorskip("nats")

NATS_URL = "nats://127.0.0.1:4222"


async def _connect_or_skip():
    """Connect to the live server, or skip loudly (never a false green)."""
    try:
        nc = await asyncio.wait_for(nats.connect(NATS_URL), timeout=3)
    except Exception as e:  # connection refused / timeout / DNS
        pytest.skip(
            f"SKIP: no NATS at {NATS_URL} ({type(e).__name__}: {e}) — "
            "JetStream dedup contract NOT verified"
        )
    if not nc.is_connected:
        await nc.close()
        pytest.skip(f"SKIP: NATS connect at {NATS_URL} did not reach connected")
    return nc


async def _scratch_stream(nc, duplicate_window: int = 120):
    """A private workqueue stream mirroring UC_SUBTASKS' dedup config."""
    name = f"T4_DEDUP_{uuid.uuid4().hex[:8]}"
    prefix = f"t4.dedup.{name.lower()}"
    js = nc.jetstream()
    await js.add_stream(
        name=name,
        subjects=[f"{prefix}.>"],
        retention="workqueue",
        duplicate_window=duplicate_window,
    )
    return js, name, f"{prefix}.execute"


async def _messages(js, name: str) -> int:
    return (await js.stream_info(name)).state.messages


@pytest.mark.integration
async def test_uc_subtasks_stream_carries_the_dedup_window_dispatchers_rely_on():
    """The dispatchers set Nats-Msg-Id expecting this window to exist.

    If someone recreates UC_SUBTASKS without duplicate_window, dispatch-side
    dedup silently stops working — this test is the tripwire.
    """
    nc = await _connect_or_skip()
    try:
        js = nc.jetstream()
        try:
            info = await js.stream_info("UC_SUBTASKS")
        except Exception as e:
            pytest.skip(f"SKIP: UC_SUBTASKS stream not present on this server ({e})")
        assert info.config.duplicate_window is not None, (
            "UC_SUBTASKS has no duplicate_window — Nats-Msg-Id dedup would be a no-op"
        )
        assert info.config.duplicate_window.total_seconds() >= 1, (
            f"duplicate_window too small to dedup a redelivery: {info.config.duplicate_window}"
        )
        assert str(info.config.retention).endswith(("workqueue", "WorkQueue")), (
            f"UC_SUBTASKS retention changed: {info.config.retention}"
        )
    finally:
        await nc.close()


@pytest.mark.integration
async def test_resends_of_one_dispatch_collapse_but_a_new_attempt_does_not():
    """The core T4 guarantee, both directions.

    Three publishes of ONE attempt (same Nats-Msg-Id) must leave a single
    stored message; a fourth publish carrying a NEW attempt's key must NOT be
    swallowed — otherwise T3's fence → READY → re-dispatch would be deduped
    into oblivion and the retry would never execute.
    """
    nc = await _connect_or_skip()
    js, name, subject = await _scratch_stream(nc)
    try:
        for _ in range(3):
            await nc.publish(
                subject, b'{"attempt_id":"0"}', headers={"Nats-Msg-Id": "k-attempt-0"}
            )
        await asyncio.sleep(0.5)
        assert await _messages(js, name) == 1, (
            "re-sends of the same Nats-Msg-Id must collapse to one stored message"
        )

        # A retry mints attempt 1 → new key → must survive dedup.
        await nc.publish(
            subject, b'{"attempt_id":"1"}', headers={"Nats-Msg-Id": "k-attempt-1"}
        )
        await asyncio.sleep(0.5)
        assert await _messages(js, name) == 2, (
            "a new attempt must not be deduped against the previous one"
        )
    finally:
        try:
            await js.delete_stream(name)
        except Exception:
            pass
        await nc.close()


@pytest.mark.integration
async def test_without_msg_id_there_is_no_dedup():
    """Documents why the header is load-bearing, not decorative.

    Same payload, no Nats-Msg-Id → the broker stores every copy. This is the
    pre-T4 state of every dispatch publisher, and it is why simply re-publishing
    used to reach a worker twice.
    """
    nc = await _connect_or_skip()
    js, name, subject = await _scratch_stream(nc)
    try:
        for _ in range(2):
            await nc.publish(subject, b'{"attempt_id":"0"}')
        await asyncio.sleep(0.5)
        assert await _messages(js, name) == 2, (
            "without Nats-Msg-Id the broker must keep every copy — "
            "if this changed, the dedup assumptions need re-checking"
        )
    finally:
        try:
            await js.delete_stream(name)
        except Exception:
            pass
        await nc.close()
