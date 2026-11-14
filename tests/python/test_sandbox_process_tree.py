"""A killed sandbox command must take its descendants with it (T65).

The agent CLI spawns tool subprocesses. Killing only the direct child leaves those
running: the worker slot is free but the CPU/GPU work is not, and on Windows that was the
behaviour for every timeout or cancellation that did not carry a cancel file.

The test does not probe PIDs (no psutil here): the grandchild appends to a file every 20 ms,
so "did it survive" is measured as "did the file keep growing".
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

from ultimate_coders.agent.sandbox import _kill_process_tree

GRANDCHILD = (
    "import pathlib, sys, time\n"
    "beat = pathlib.Path(sys.argv[1])\n"
    "for _ in range(2000):\n"
    "    with beat.open('a') as handle:\n"
    "        handle.write('x')\n"
    "    time.sleep(0.02)\n"
)

CHILD = (
    "import subprocess, sys\n"
    "subprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
)


def _size(path: pathlib.Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


async def test_killing_a_command_takes_its_descendants(tmp_path):
    beat = tmp_path / "beat.txt"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CHILD,
        GRANDCHILD,
        str(beat),
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while _size(beat) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert _size(beat) >= 2, "the grandchild never started"

        _kill_process_tree(proc)
        await asyncio.wait_for(proc.wait(), timeout=10)

        after_kill = _size(beat)
        await asyncio.sleep(0.5)
        assert _size(beat) == after_kill, "a descendant survived the kill and kept working"
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
