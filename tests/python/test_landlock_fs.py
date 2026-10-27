"""Landlock FS enforcement at agent-child spawn (T17 / D8).

Skip matrix (live FS probe):
- Runs when ``platform_supports_landlock()`` is True (Linux + Landlock ABI ≥ 1).
- Skips on macOS / Windows / kernels without Landlock.

Unit tests use the injectable applicator and never require Landlock.

Dual-flag regression: when both NetworkMode and Landlock enforce are on with
``network=none``, net block + FS write deny both hold (live probe, skip-gated).
"""

from __future__ import annotations

import socket
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from ultimate_coders.agent.landlock import (  # noqa: E402
    LANDLOCK_ABI_BACKEND,
    LANDLOCK_ENFORCE_ENV,
    LandlockApplyPlan,
    LandlockError,
    apply_landlock_argv,
    clear_landlock_applicator,
    landlock_enforce_enabled,
    plan_landlock,
    platform_supports_landlock,
    reset_landlock_state_for_tests,
    resolve_landlock_paths,
    set_landlock_applicator,
)
from ultimate_coders.agent.network_mode import (  # noqa: E402
    NETWORK_MODE_ENFORCE_ENV,
    platform_supports_unshare_netns,
    reset_network_mode_state_for_tests,
)
from ultimate_coders.agent.sandbox import (  # noqa: E402
    NetworkMode,
    SandboxConfig,
    SandboxManager,
)


@pytest.fixture(autouse=True)
def _reset_landlock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(LANDLOCK_ENFORCE_ENV, raising=False)
    monkeypatch.delenv(NETWORK_MODE_ENFORCE_ENV, raising=False)
    reset_landlock_state_for_tests()
    reset_network_mode_state_for_tests()
    yield
    reset_landlock_state_for_tests()
    reset_network_mode_state_for_tests()


# ── flag ─────────────────────────────────────────────────────────


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LANDLOCK_ENFORCE_ENV, raising=False)
    assert landlock_enforce_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on", "On"])
def test_flag_truthy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(LANDLOCK_ENFORCE_ENV, raw)
    assert landlock_enforce_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "maybe"])
def test_flag_falsy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(LANDLOCK_ENFORCE_ENV, raw)
    assert landlock_enforce_enabled() is False


# ── plan matrix ──────────────────────────────────────────────────


def test_plan_noop_when_flag_off(tmp_path: Path) -> None:
    plan = plan_landlock(str(tmp_path), enforce=False)
    assert plan.backend == "noop"
    assert plan.argv_prefix == ()


def test_plan_fail_closed_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "ultimate_coders.agent.landlock.platform_supports_landlock",
        lambda: False,
    )
    with pytest.raises(LandlockError, match="Refusing start"):
        plan_landlock(str(tmp_path), enforce=True)


def test_plan_fail_closed_on_empty_working_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ultimate_coders.agent.landlock.platform_supports_landlock",
        lambda: True,
    )
    with pytest.raises(LandlockError, match="working_dir"):
        plan_landlock("", enforce=True)


def test_plan_fail_closed_on_missing_working_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "ultimate_coders.agent.landlock.platform_supports_landlock",
        lambda: True,
    )
    missing = tmp_path / "does-not-exist"
    with pytest.raises(LandlockError, match="missing|not a directory"):
        plan_landlock(str(missing), enforce=True)


def test_plan_uses_landlock_abi_when_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "ultimate_coders.agent.landlock.platform_supports_landlock",
        lambda: True,
    )
    monkeypatch.setattr(
        "ultimate_coders.agent.landlock.landlock_abi_version",
        lambda: 6,
    )
    plan = plan_landlock(str(tmp_path), enforce=True)
    assert plan.backend == LANDLOCK_ABI_BACKEND
    assert plan.argv_prefix[0] == sys.executable
    assert plan.argv_prefix[1].endswith("landlock.py")
    assert "--" in plan.argv_prefix
    assert "/" in plan.ro_paths
    assert str(tmp_path.resolve()) in plan.rw_paths
    assert "/tmp" in plan.rw_paths


def test_injected_applicator_overrides_builtin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "ultimate_coders.agent.landlock.platform_supports_landlock",
        lambda: False,
    )

    def _fake(_wd: str) -> LandlockApplyPlan:
        return LandlockApplyPlan(
            argv_prefix=("env", "--"),
            backend="injected",
        )

    set_landlock_applicator(_fake)
    plan = plan_landlock(str(tmp_path), enforce=True)
    assert plan.backend == "injected"
    assert apply_landlock_argv("python", ["-c", "pass"], plan) == (
        "env",
        ["--", "python", "-c", "pass"],
    )
    clear_landlock_applicator()


def test_apply_landlock_argv_noop_and_prefix() -> None:
    noop = LandlockApplyPlan()
    assert apply_landlock_argv("cmd", ["a", "b"], noop) == ("cmd", ["a", "b"])
    wrapped = LandlockApplyPlan(
        argv_prefix=("python", "/tmp/landlock.py", "--"),
        backend=LANDLOCK_ABI_BACKEND,
    )
    assert apply_landlock_argv("cmd", ["a"], wrapped) == (
        "python",
        ["/tmp/landlock.py", "--", "cmd", "a"],
    )


def test_resolve_paths_includes_tmpdir_and_extras(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extra_rw = tmp_path / "extra-rw"
    extra_ro = tmp_path / "extra-ro"
    extra_rw.mkdir()
    extra_ro.mkdir()
    custom_tmp = tmp_path / "custom-tmp"
    custom_tmp.mkdir()
    env = {
        "TMPDIR": str(custom_tmp),
        "UC_LANDLOCK_EXTRA_RW": str(extra_rw),
        "UC_LANDLOCK_EXTRA_RO": str(extra_ro),
    }
    ro, rw = resolve_landlock_paths(str(tmp_path), environ=env)
    assert "/" in ro
    assert str(extra_ro.resolve()) in ro
    assert str(tmp_path.resolve()) in rw
    assert "/tmp" in rw
    assert str(custom_tmp.resolve()) in rw
    assert str(extra_rw.resolve()) in rw


def test_resolve_paths_ignores_relative_tmpdir(tmp_path: Path) -> None:
    """Relative TMPDIR must not become an RW root (absolute-only contract)."""
    ro, rw = resolve_landlock_paths(
        str(tmp_path),
        environ={"TMPDIR": "relative-tmp-should-ignore"},
    )
    assert "/" in ro
    assert str(tmp_path.resolve()) in rw
    assert "/tmp" in rw
    assert all("relative-tmp-should-ignore" not in p for p in rw)


# ── sandbox choke point ──────────────────────────────────────────


def _manager(**kwargs) -> SandboxManager:
    defaults = {
        "project_path": tempfile.gettempdir(),
        "network": NetworkMode.FULL,
        "warm_pool_size": 0,
    }
    defaults.update(kwargs)
    return SandboxManager(SandboxConfig(**defaults))


def _probe_request(working_dir: str | None = None, **overrides) -> dict:
    request = {
        "command": sys.executable,
        "args": ["-c", "print('ok')"],
        "timeout_secs": 30,
        "env_vars": {},
        "working_dir": working_dir or tempfile.gettempdir(),
    }
    request.update(overrides)
    return request


@pytest.mark.asyncio
async def test_subprocess_unchanged_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(LANDLOCK_ENFORCE_ENV, raising=False)
    manager = _manager(project_path=str(tmp_path))
    result = await manager._execute_subprocess(
        _probe_request(working_dir=str(tmp_path))
    )
    assert result.exit_code == 0
    assert "ok" in result.stdout


@pytest.mark.asyncio
async def test_subprocess_fail_closed_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(LANDLOCK_ENFORCE_ENV, "1")
    monkeypatch.setattr(
        "ultimate_coders.agent.landlock.platform_supports_landlock",
        lambda: False,
    )
    manager = _manager(project_path=str(tmp_path))
    result = await manager._execute_subprocess(
        _probe_request(working_dir=str(tmp_path))
    )
    assert result.exit_code == -1
    assert "Landlock" in result.stderr or "landlock" in result.stderr.lower()
    assert "Refusing" in result.stderr


@pytest.mark.asyncio
async def test_subprocess_injected_wraps_after_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Landlock wrap is innermost; NetworkMode applied outer (after NNP)."""
    monkeypatch.setenv(LANDLOCK_ENFORCE_ENV, "1")
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")

    seen: dict[str, object] = {}
    composed: dict[str, object] = {}

    from ultimate_coders.agent import network_mode as net_mod
    from ultimate_coders.agent.network_mode import NetworkApplyPlan

    def _net_app(mode: str) -> NetworkApplyPlan:
        seen["net_mode"] = mode
        return NetworkApplyPlan(
            mode=mode,
            argv_prefix=("env", "UC_NET_WRAP=1"),
            backend="injected",
        )

    def _ll_app(wd: str) -> LandlockApplyPlan:
        seen["ll_wd"] = wd
        return LandlockApplyPlan(
            argv_prefix=("env", "UC_LL_WRAP=1"),
            backend="injected",
        )

    # Spy on apply helpers to assert ordering without relying on nested
    # ``env --`` quirks across platforms.
    real_apply_net = net_mod.apply_network_argv
    real_apply_ll = apply_landlock_argv

    def _spy_net(command, args, plan):
        composed["before_net"] = (command, list(args), plan.backend)
        out = real_apply_net(command, args, plan)
        composed["after_net"] = out
        return out

    def _spy_ll(command, args, plan):
        composed["before_ll"] = (command, list(args), plan.backend)
        out = real_apply_ll(command, args, plan)
        composed["after_ll"] = out
        return out

    monkeypatch.setattr(
        "ultimate_coders.agent.sandbox.apply_network_argv",
        _spy_net,
    )
    monkeypatch.setattr(
        "ultimate_coders.agent.sandbox.apply_landlock_argv",
        _spy_ll,
    )

    net_mod.set_network_mode_applicator(_net_app)
    set_landlock_applicator(_ll_app)

    manager = _manager(project_path=str(tmp_path), network=NetworkMode.NONE)
    result = await manager._execute_subprocess(
        _probe_request(
            working_dir=str(tmp_path),
            args=[
                "-c",
                "import os; print(os.environ.get('UC_LL_WRAP',''), "
                "os.environ.get('UC_NET_WRAP',''))",
            ],
        )
    )
    assert result.exit_code == 0, result.stderr
    # Both env markers visible; NetworkMode is outermost (applied last).
    assert "1" in result.stdout
    assert seen["net_mode"] == "none"
    assert seen["ll_wd"] == str(tmp_path)
    assert composed["before_ll"][0] == sys.executable  # bare agent first
    assert composed["after_ll"][0] == "env"  # landlock inner
    assert composed["before_net"][0] == "env"  # then network sees landlock
    assert composed["after_net"][0] == "env"  # network outermost
    assert composed["before_net"][2] == "injected"


@pytest.mark.asyncio
async def test_execute_skips_engine_sandbox_when_landlock_enforce_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(LANDLOCK_ENFORCE_ENV, "1")
    monkeypatch.setattr(
        "ultimate_coders.agent.landlock.platform_supports_landlock",
        lambda: False,
    )

    class _Engine:
        def __init__(self) -> None:
            self.called = False

        async def execute_in_sandbox(self, **_kwargs):
            self.called = True
            return {
                "exit_code": 0,
                "stdout": "engine",
                "stderr": "",
                "duration_ms": 1,
                "timed_out": False,
            }

    class _Adapter:
        def name(self) -> str:
            return "grok-build"

        def build_request(self, prompt, wd, config, subtask_config=None):
            return {
                "command": sys.executable,
                "args": ["-c", "print('child')"],
                "timeout_secs": 30,
                "env_vars": {},
                "working_dir": wd,
            }

        def parse_output(self, result):
            from ultimate_coders.agent.sandbox import AgentOutput

            return AgentOutput(
                summary=result.stdout or result.stderr,
                success=result.exit_code == 0,
            )

    engine = _Engine()
    config = SandboxConfig(
        project_path=str(tmp_path),
        network=NetworkMode.FULL,
        warm_pool_size=0,
    )
    manager = SandboxManager(config, engine=engine)
    monkeypatch.setattr(manager, "_adapter", _Adapter())
    monkeypatch.setattr(manager, "_create_adapter", lambda _agent: _Adapter())
    output = await manager.execute("hi")
    assert engine.called is False
    assert output.success is False
    assert "Landlock" in output.summary or "landlock" in output.summary.lower()


def test_network_mode_plan_still_independent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Landlock off must not change NetworkMode planning."""
    from ultimate_coders.agent.network_mode import (
        apply_network_argv,
        plan_network_mode,
    )

    monkeypatch.delenv(LANDLOCK_ENFORCE_ENV, raising=False)
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")
    monkeypatch.setattr(
        "ultimate_coders.agent.network_mode.platform_supports_unshare_netns",
        lambda: True,
    )
    net = plan_network_mode("none", enforce=True)
    ll = plan_landlock(str(tmp_path), enforce=False)
    assert net.backend == "unshare_netns"
    assert ll.backend == "noop"
    # Compose as sandbox does: landlock inner (noop), network outer.
    cmd, args = apply_landlock_argv("agent", ["--x"], ll)
    cmd, args = apply_network_argv(cmd, args, net)
    assert cmd == "unshare"
    assert args[-2:] == ["agent", "--x"]


# ── live probes ──────────────────────────────────────────────────


_FS_PROBE = r"""
import os, sys, errno
from pathlib import Path
wd = Path(os.environ["UC_LL_PROBE_WD"])
outside = Path(os.environ["UC_LL_PROBE_OUTSIDE"])
try:
    (wd / "inside.txt").write_text("ok")
except OSError as e:
    print("INSIDE_FAIL", e.errno)
    sys.exit(3)
try:
    outside.write_text("nope")
    print("OUTSIDE_WRITE_OK")
    sys.exit(1)
except OSError as e:
    print("OUTSIDE_DENIED", e.errno == errno.EACCES, e.errno)
    sys.exit(0)
"""


@pytest.mark.asyncio
async def test_live_landlock_denies_write_outside_rw_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Live probe: enforce on → write outside RW root denied; inside OK.

    Documented skip: non-Linux / Landlock ABI unavailable.
    """
    if not platform_supports_landlock():
        pytest.skip("Landlock ABI unavailable (CI skip matrix)")

    monkeypatch.setenv(LANDLOCK_ENFORCE_ENV, "1")
    work = tmp_path / "work"
    work.mkdir()
    # Outside path under /var/tmp or sibling of work but not under work/tmp.
    # Use a path under /etc's parent that is NOT /tmp (RW). Prefer work.parent
    # only if it is not itself an RW root — work.parent is tmp_path which is
    # under /tmp on many systems (and /tmp is RW). Use /var when writable by
    # parent for the outside probe file path expectation; create under
    # a directory that is readable but not in RW set: sibling via /dev/shm
    # is often RW via tmpfs and may be under / — still RO for write unless
    # we pick carefully.
    #
    # RO / allows read of /etc; write to a new file under /var/tmp may work
    # if /tmp-like. Safest outside target: a file under /proc is not
    # creatable; use /etc/… which should EACCES under Landlock write deny.
    outside = "/etc/uc-landlock-probe-should-fail"
    manager = _manager(project_path=str(work))
    result = await manager._execute_subprocess(
        _probe_request(
            working_dir=str(work),
            env_vars={
                "UC_LL_PROBE_WD": str(work),
                "UC_LL_PROBE_OUTSIDE": outside,
            },
            args=["-c", _FS_PROBE],
        )
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "OUTSIDE_DENIED" in result.stdout
    assert (work / "inside.txt").read_text() == "ok"


def _parent_can_egress() -> bool:
    try:
        sock = socket.create_connection(("1.1.1.1", 53), timeout=2)
        sock.close()
        return True
    except OSError:
        return False


_EGRESS_PROBE = r"""
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect(("1.1.1.1", 53))
    print("REACHABLE")
    sys.exit(0)
except OSError as e:
    print("BLOCKED", getattr(e, "errno", None))
    sys.exit(2)
finally:
    s.close()
"""


@pytest.mark.asyncio
async def test_live_dual_enforce_fs_and_network_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Both enforces on: FS write outside denied AND network none blocks.

    Documented skip: missing Landlock, missing unshare, or no parent egress.
    """
    if not platform_supports_landlock():
        pytest.skip("Landlock ABI unavailable (CI skip matrix)")
    if not platform_supports_unshare_netns():
        pytest.skip("unshare user+net ns unavailable (CI skip matrix)")
    if not _parent_can_egress():
        pytest.skip("parent has no external TCP egress for comparison")

    monkeypatch.setenv(LANDLOCK_ENFORCE_ENV, "1")
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")

    work = tmp_path / "work"
    work.mkdir()
    manager = _manager(project_path=str(work), network=NetworkMode.NONE)

    net_result = await manager._execute_subprocess(
        _probe_request(working_dir=str(work), args=["-c", _EGRESS_PROBE])
    )
    assert net_result.exit_code != 0
    assert "BLOCKED" in net_result.stdout or "BLOCKED" in net_result.stderr

    fs_result = await manager._execute_subprocess(
        _probe_request(
            working_dir=str(work),
            env_vars={
                "UC_LL_PROBE_WD": str(work),
                "UC_LL_PROBE_OUTSIDE": "/etc/uc-landlock-probe-should-fail",
            },
            args=["-c", _FS_PROBE],
        )
    )
    assert fs_result.exit_code == 0, (fs_result.stdout, fs_result.stderr)
    assert "OUTSIDE_DENIED" in fs_result.stdout
    assert _parent_can_egress() is True


def test_helpers_do_not_touch_worker_sockets() -> None:
    """Unit guard: landlock planner has no socket.connect side effects."""
    import ast
    import inspect

    import ultimate_coders.agent.landlock as mod

    src = inspect.getsource(mod)
    tree = ast.parse(src)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"connect", "create_connection"}
    ]
    assert calls == []
