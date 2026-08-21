"""Regression coverage for the Dashboard's persistent TUI process."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from ultimate_coders.dashboard.app import DashboardApp


@pytest.mark.skipif(
    os.name == "nt", reason="util-linux `script` is a POSIX-only PTY path"
)
def test_linux_tui_script_wrapper_starts_omp_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Linux `script` path must not fall through to the pipe fallback.

    A missing return after the first ``Popen`` launched two independent OMP
    sessions for one WebSocket connection, so their terminal input competed.
    """
    omp_script = tmp_path / "uc-omp.sh"
    omp_script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("UC_OMP_SCRIPT", str(omp_script))
    monkeypatch.setattr(
        "ultimate_coders.dashboard.app.shutil.which",
        lambda name: "/usr/bin/script" if name == "script" else None,
    )
    process = MagicMock(pid=4242)
    popen = MagicMock(return_value=process)
    monkeypatch.setattr("ultimate_coders.dashboard.app.subprocess.Popen", popen)

    app = DashboardApp(orchestrator=MagicMock())

    assert app._start_tui_pty() is process
    assert app._tui_pty is process
    popen.assert_called_once()
    assert popen.call_args.args[0] == [
        "/usr/bin/script",
        "-q",
        "-c",
        str(omp_script),
        os.devnull,
    ]
