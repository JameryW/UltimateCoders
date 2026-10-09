"""The live latency gate must never pass on loss, duplication or slow samples."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-sse-latency.py"
SPEC = importlib.util.spec_from_file_location("sse_latency_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def test_complete_delivery_reports_ordered_samples_and_nearest_rank_percentiles():
    result = gate.summarize({2: 8.0, 0: 2.0, 1: 5.0}, 3, 0, 200.0)
    assert result["passed"] is True
    assert result["latencies_ms"] == [2.0, 5.0, 8.0]
    assert result["p50_ms"] == 5.0
    assert result["p95_ms"] == result["max_ms"] == 8.0


def test_missing_delivery_is_a_failure_even_with_fast_observed_samples():
    result = gate.summarize({0: 1.0, 2: 1.0}, 3, 0, 200.0)
    assert result["passed"] is False
    assert result["missing"] == [1]
    assert result["latencies_ms"] == [1.0, None, 1.0]
    empty = gate.summarize({}, 3, 0, 200.0)
    assert empty["passed"] is False
    assert empty["p99_ms"] is None


@pytest.mark.parametrize("slow_sample", [200.0, 201.0, float("nan"), float("inf"), -1.0])
def test_a_single_invalid_or_slow_sample_cannot_hide_behind_a_fast_percentile(slow_sample):
    samples = dict.fromkeys(range(300), 1.0)
    samples[299] = slow_sample
    assert gate.summarize(samples, 300, 0, 200.0)["passed"] is False


def test_duplicate_delivery_is_a_failure_even_without_loss():
    assert gate.summarize({0: 1.0}, 1, 1, 200.0)["passed"] is False


@pytest.mark.parametrize("value", ["nan", "inf", "-0.1"])
def test_nonfinite_or_negative_configuration_is_rejected(value):
    with pytest.raises(argparse.ArgumentTypeError):
        gate.finite_number(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate,stalled_close", [(False, False), (True, False), (False, True)])
async def test_measure_reads_fragmented_multiline_frames_and_closes_transports(
    monkeypatch, duplicate, stalled_close
):
    streams = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self):
            self.queue = asyncio.Queue()
            self.closed = False

        async def __aiter__(self):
            while True:
                frame = await self.queue.get()
                # A frame may span network chunks; JSON may span SSE data lines.
                yield frame[:7]
                yield frame[7:]

        async def aclose(self):
            self.closed = True

    async def handler(request):
        stream = Stream()
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    class Publisher:
        closed = False

        async def publish(self, subject, raw):
            assert subject == "uc.task.event"
            payload = json.loads(raw)
            frame = (
                ": heartbeat\r\n\r\nevent: task_event\r\n"
                'data: {\r\ndata: "data": ' + json.dumps(payload["data"]) + "}\r\n\r\n"
            ).encode()
            for stream in streams:
                stream.queue.put_nowait(frame)
                if duplicate and payload["data"]["sequence"] == 0:
                    stream.queue.put_nowait(frame)

        async def flush(self, timeout):
            pass

        async def close(self):
            if stalled_close:
                await asyncio.Event().wait()
            self.closed = True

    publisher = Publisher()

    async def connect(*args, **kwargs):
        return publisher

    client_type = httpx.AsyncClient
    monkeypatch.setattr(gate.nats, "connect", connect)
    monkeypatch.setattr(
        gate.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    args = argparse.Namespace(
        dashboard_url="http://dashboard",
        nats_url="nats://broker",
        clients=2,
        events=3,
        interval_ms=0,
        max_latency_ms=10000,
        timeout=0.2 if stalled_close else 2,
        token_env="UC_TEST_UNUSED_TOKEN",
    )
    if stalled_close:
        task = asyncio.create_task(gate.measure(args))
        try:
            done, _ = await asyncio.wait([task], timeout=2)
            assert task in done, "the gate must enforce its own cleanup deadline"
            with pytest.raises(asyncio.TimeoutError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert all(stream.closed for stream in streams)
        return
    result = await gate.measure(args)
    assert result["passed"] is (not duplicate)
    assert result["errors"] == []
    assert all(item["received"] == 3 for item in result["clients"])
    assert all(item["duplicates"] == int(duplicate) for item in result["clients"])
    assert publisher.closed is True
    assert len(streams) == 2 and all(stream.closed for stream in streams)
