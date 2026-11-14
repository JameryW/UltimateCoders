"""Streaming must obey the same total timeout as a silent command (T64).

`timeout_secs` bounds the whole command on the `communicate()` path. The streaming path
used to wait that long *per stdout line*, so a command that keeps printing reset the clock
on every line and never hit the limit — the budget was an idle timeout, not a budget, and
the worker slot it exists to bound stayed occupied.
"""

from __future__ import annotations

import sys

import pytest
from ultimate_coders.agent.sandbox import SandboxConfig, SandboxManager

CHATTY_SCRIPT = (
    "import sys, time\n"
    "for i in range(40):\n"
    "    print(i, flush=True)\n"
    "    time.sleep(0.05)\n"
)


@pytest.fixture
def manager(tmp_path) -> SandboxManager:
    return SandboxManager(SandboxConfig(agent="claude-code", project_path=str(tmp_path)))


def _request(tmp_path, *, timeout_secs: float, script: str) -> dict:
    return {
        "command": sys.executable,
        "args": ["-c", script],
        "timeout_secs": timeout_secs,
        "env_vars": {},
        "working_dir": str(tmp_path),
    }


async def test_a_chatty_command_still_hits_the_total_timeout(tmp_path, manager):
    lines: list[str] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    result = await manager._execute_subprocess(
        _request(tmp_path, timeout_secs=0.4, script=CHATTY_SCRIPT),
        on_stdout_line=on_line,
        agent="claude-code",
    )

    assert result.timed_out is True
    assert result.exit_code == -1
    assert result.duration_ms < 1500, "the total budget stopped it, not the end of the script"
    assert lines, "lines streamed before the deadline are kept"
    assert len(lines) < 40, "it was stopped mid-stream"


async def test_a_quiet_command_completes_normally(tmp_path, manager):
    lines: list[str] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    result = await manager._execute_subprocess(
        _request(tmp_path, timeout_secs=30, script="print('done', flush=True)\n"),
        on_stdout_line=on_line,
        agent="claude-code",
    )

    assert result.timed_out is False
    assert result.exit_code == 0
    assert lines == ["done"]
