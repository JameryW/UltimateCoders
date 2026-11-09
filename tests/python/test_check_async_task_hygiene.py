"""`create_task` reference hygiene (T49).

CPython holds tasks weakly: an unreferenced task can be garbage-collected before it
finishes and its exception surfaces only as "Task exception was never retrieved". This
guard decides the decidable half -- that each `create_task(...)` keeps a reference it can
be awaited, gathered, kept or cancelled through.

The tests below prove the guard can say **no** (a lost task is reported) as well as yes,
and that the real repository is clean.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "check-async-task-hygiene.py"
TARGET = REPO / "python" / "ultimate_coders"


def _load_guard():
    spec = importlib.util.spec_from_file_location("async_task_hygiene_guard", GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scan(tmp_path: pathlib.Path, source: str) -> list[tuple[int, str]]:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return _load_guard().scan(path)


def test_an_awaited_task_is_kept(tmp_path):
    findings = _scan(
        tmp_path,
        "import asyncio\n"
        "async def run():\n"
        "    task = asyncio.create_task(work())\n"
        "    await task\n",
    )
    assert findings == []


def test_a_task_with_a_done_callback_is_kept(tmp_path):
    findings = _scan(
        tmp_path,
        "import asyncio\n"
        "async def run():\n"
        "    task = asyncio.create_task(work())\n"
        "    task.add_done_callback(lambda _: None)\n",
    )
    assert findings == []


def test_a_task_in_a_set_is_kept(tmp_path):
    findings = _scan(
        tmp_path,
        "import asyncio\n"
        "async def run(pending):\n"
        "    task = asyncio.create_task(work())\n"
        "    pending.add(task)\n",
    )
    assert findings == []


def test_a_shielded_task_is_kept(tmp_path):
    findings = _scan(
        tmp_path,
        "import asyncio\n"
        "async def run():\n"
        "    write = asyncio.create_task(write_state())\n"
        "    await asyncio.shield(write)\n",
    )
    assert findings == []


def test_a_task_stored_on_self_is_kept(tmp_path):
    findings = _scan(
        tmp_path,
        "import asyncio\n"
        "class Holder:\n"
        "    async def start(self):\n"
        "        self._task = asyncio.create_task(work())\n",
    )
    assert findings == []


def test_a_dropped_task_is_reported(tmp_path):
    findings = _scan(
        tmp_path,
        "import asyncio\n"
        "async def run():\n"
        "    task = asyncio.create_task(work())\n"
        "    return None\n",
    )
    assert findings == [(3, "task is never kept, awaited, gathered or cancelled")]


def test_a_loop_create_task_is_reported_too(tmp_path):
    findings = _scan(
        tmp_path,
        "import asyncio\n"
        "def run(loop):\n"
        "    task = loop.create_task(work())\n",
    )
    assert findings == [(3, "task is never kept, awaited, gathered or cancelled")]


def test_an_inline_task_is_not_a_finding(tmp_path):
    findings = _scan(
        tmp_path,
        "import asyncio\n"
        "def start():\n"
        "    return asyncio.create_task(work())\n",
    )
    assert findings == [], "a returned task is owned by the caller"


def test_the_real_repository_is_clean():
    result = subprocess.run(
        [str(REPO / ".venv" / "Scripts" / "python.exe"), str(GUARD)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    assert "async task hygiene check passed." in result.stdout


def test_the_guard_actually_walks_the_package():
    modules = sorted(TARGET.rglob("*.py"))
    assert len(modules) >= 100, f"only {len(modules)} module(s) found under python/ultimate_coders"
