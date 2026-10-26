"""NetworkMode real enforcement at agent-child spawn (T15 / D8 Seam H).

Skip matrix (live egress probe):
- Runs when ``platform_supports_unshare_netns()`` is True (Linux + rootless
  ``unshare --user --map-root-user --net``).
- Skips on Windows, missing ``unshare``, or userns-disabled runners.

Unit tests use the injectable applicator and never require root.
"""

from __future__ import annotations

import socket
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from ultimate_coders.agent.network_mode import (  # noqa: E402
    NETWORK_MODE_ENFORCE_ENV,
    UNSHARE_NETNS_PREFIX,
    NetworkApplyPlan,
    NetworkModeError,
    apply_network_argv,
    clear_network_mode_applicator,
    network_mode_enforce_enabled,
    normalize_network_mode,
    plan_network_mode,
    platform_supports_unshare_netns,
    reset_network_mode_state_for_tests,
    set_network_mode_applicator,
    set_restricted_applicator,
)
from ultimate_coders.agent.sandbox import (  # noqa: E402
    NetworkMode,
    SandboxConfig,
    SandboxManager,
)


@pytest.fixture(autouse=True)
def _reset_network_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(NETWORK_MODE_ENFORCE_ENV, raising=False)
    reset_network_mode_state_for_tests()
    yield
    reset_network_mode_state_for_tests()


# ── flag + normalize ─────────────────────────────────────────────


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(NETWORK_MODE_ENFORCE_ENV, raising=False)
    assert network_mode_enforce_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on", "On"])
def test_flag_truthy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, raw)
    assert network_mode_enforce_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "maybe"])
def test_flag_falsy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, raw)
    assert network_mode_enforce_enabled() is False


def test_normalize_network_mode() -> None:
    assert normalize_network_mode("NONE") == "none"
    assert normalize_network_mode("Restricted") == "restricted"
    assert normalize_network_mode("full") == "full"
    with pytest.raises(ValueError):
        normalize_network_mode("wifi")


# ── plan matrix ──────────────────────────────────────────────────


def test_plan_noop_when_flag_off_for_all_modes() -> None:
    for mode in ("none", "restricted", "full"):
        plan = plan_network_mode(mode, enforce=False)
        assert plan.backend == "noop"
        assert plan.argv_prefix == ()
        assert plan.mode == mode


def test_plan_full_noop_when_enforce_on() -> None:
    plan = plan_network_mode("full", enforce=True)
    assert plan == NetworkApplyPlan(mode="full", argv_prefix=(), backend="noop")


def test_plan_none_uses_unshare_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ultimate_coders.agent.network_mode.platform_supports_unshare_netns",
        lambda: True,
    )
    plan = plan_network_mode("none", enforce=True)
    assert plan.mode == "none"
    assert plan.backend == "unshare_netns"
    assert plan.argv_prefix == UNSHARE_NETNS_PREFIX


def test_plan_none_fail_closed_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ultimate_coders.agent.network_mode.platform_supports_unshare_netns",
        lambda: False,
    )
    with pytest.raises(NetworkModeError, match="Refusing start"):
        plan_network_mode("none", enforce=True)


def test_plan_restricted_fail_closed_without_applicator() -> None:
    with pytest.raises(NetworkModeError, match="no Restricted applicator"):
        plan_network_mode("restricted", enforce=True)


def test_plan_restricted_uses_registered_applicator() -> None:
    def _restricted(_mode: str) -> NetworkApplyPlan:
        return NetworkApplyPlan(
            mode="restricted",
            argv_prefix=("echo", "--allowlist-proxy", "--"),
            backend="injected",
        )

    set_restricted_applicator(_restricted)
    plan = plan_network_mode("restricted", enforce=True)
    assert plan.backend == "injected"
    assert plan.argv_prefix[0] == "echo"


def test_plan_restricted_param_overrides_registry() -> None:
    set_restricted_applicator(lambda _m: NetworkApplyPlan(mode="restricted", backend="registry"))

    def _param(_mode: str) -> NetworkApplyPlan:
        return NetworkApplyPlan(
            mode="restricted",
            argv_prefix=("wrap", "--"),
            backend="param",
        )

    plan = plan_network_mode(
        "restricted",
        enforce=True,
        restricted_applicator=_param,
    )
    assert plan.backend == "param"


def test_injected_applicator_overrides_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ultimate_coders.agent.network_mode.platform_supports_unshare_netns",
        lambda: False,
    )

    def _fake(mode: str) -> NetworkApplyPlan:
        return NetworkApplyPlan(
            mode=mode,
            argv_prefix=("fake-wrap", "--"),
            backend="injected",
        )

    set_network_mode_applicator(_fake)
    plan = plan_network_mode("none", enforce=True)
    assert plan.backend == "injected"
    assert apply_network_argv("python", ["-c", "pass"], plan) == (
        "fake-wrap",
        ["--", "python", "-c", "pass"],
    )
    clear_network_mode_applicator()


def test_apply_network_argv_noop_and_prefix() -> None:
    noop = NetworkApplyPlan(mode="full")
    assert apply_network_argv("cmd", ["a", "b"], noop) == ("cmd", ["a", "b"])
    wrapped = NetworkApplyPlan(
        mode="none",
        argv_prefix=("unshare", "--net", "--"),
        backend="unshare_netns",
    )
    assert apply_network_argv("cmd", ["a"], wrapped) == (
        "unshare",
        ["--net", "--", "cmd", "a"],
    )


# ── sandbox choke point ──────────────────────────────────────────


def _manager(network: str = NetworkMode.FULL) -> SandboxManager:
    return SandboxManager(SandboxConfig(project_path=tempfile.gettempdir(), network=network))


def _probe_request(**overrides) -> dict:
    request = {
        "command": sys.executable,
        "args": ["-c", "print('ok')"],
        "timeout_secs": 30,
        "env_vars": {},
        "working_dir": tempfile.gettempdir(),
    }
    request.update(overrides)
    return request


@pytest.mark.asyncio
async def test_subprocess_unchanged_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(NETWORK_MODE_ENFORCE_ENV, raising=False)
    manager = _manager(NetworkMode.NONE)
    result = await manager._execute_subprocess(_probe_request())
    assert result.exit_code == 0
    assert "ok" in result.stdout


@pytest.mark.asyncio
async def test_subprocess_full_noop_when_enforce_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")
    manager = _manager(NetworkMode.FULL)
    result = await manager._execute_subprocess(_probe_request())
    assert result.exit_code == 0
    assert "ok" in result.stdout


@pytest.mark.asyncio
async def test_subprocess_restricted_fail_closed_no_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T16 registers the Restricted applicator at SandboxManager init.

    Empty ``UC_NETWORK_ALLOWLIST`` must still fail closed (no silent Full).
    """
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")
    monkeypatch.delenv("UC_NETWORK_ALLOWLIST", raising=False)
    manager = _manager(NetworkMode.RESTRICTED)
    result = await manager._execute_subprocess(_probe_request())
    assert result.exit_code == -1
    assert "restricted" in result.stderr.lower() or "allowlist" in result.stderr.lower()
    assert (
        "Refusing" in result.stderr
        or "empty" in result.stderr.lower()
        or "allowlist" in result.stderr.lower()
    )


@pytest.mark.asyncio
async def test_subprocess_injected_none_wraps_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")
    # Wrap with env that just runs the real command — proves argv rewrite.
    set_network_mode_applicator(
        lambda mode: NetworkApplyPlan(
            mode=mode,
            argv_prefix=("env", "--"),
            backend="injected",
        )
    )
    manager = _manager(NetworkMode.NONE)
    result = await manager._execute_subprocess(_probe_request())
    assert result.exit_code == 0
    assert "ok" in result.stdout


@pytest.mark.asyncio
async def test_execute_skips_engine_sandbox_when_enforce_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rust execute_in_sandbox must not bypass NetworkMode when enforce on."""
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")

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
        project_path=tempfile.gettempdir(),
        network=NetworkMode.RESTRICTED,
        warm_pool_size=0,
    )
    manager = SandboxManager(config, engine=engine)
    # Restricted without applicator → fail closed on Python path; engine unused.
    monkeypatch.setattr(manager, "_adapter", _Adapter())
    monkeypatch.setattr(manager, "_create_adapter", lambda _agent: _Adapter())
    output = await manager.execute("hi")
    assert engine.called is False
    assert output.success is False
    assert "Restricted" in output.summary or "restricted" in output.summary.lower()


# ── live None vs Full egress probe ───────────────────────────────


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


def _parent_can_egress() -> bool:
    try:
        sock = socket.create_connection(("1.1.1.1", 53), timeout=2)
        sock.close()
        return True
    except OSError:
        return False


@pytest.mark.asyncio
async def test_live_none_blocks_child_egress_full_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seam H live probe: flag on + none blocks child; full reaches; parent ok.

    Documented skip: non-Linux / userns disabled / no egress from parent.
    """
    if not platform_supports_unshare_netns():
        pytest.skip("unshare user+net ns unavailable (CI skip matrix)")
    if not _parent_can_egress():
        pytest.skip("parent has no external TCP egress for comparison")

    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")
    request = _probe_request(args=["-c", _EGRESS_PROBE])

    full_mgr = _manager(NetworkMode.FULL)
    full_result = await full_mgr._execute_subprocess(request)
    assert full_result.exit_code == 0, full_result.stderr
    assert "REACHABLE" in full_result.stdout

    none_mgr = _manager(NetworkMode.NONE)
    none_result = await none_mgr._execute_subprocess(request)
    assert none_result.exit_code != 0
    assert "BLOCKED" in none_result.stdout or "BLOCKED" in none_result.stderr

    # Worker/parent control-plane still has egress (NATS/Gateway stand-in).
    assert _parent_can_egress() is True


def test_helpers_only_touch_child_argv_not_worker_sockets() -> None:
    """Unit guard: planning rewrites argv only; no socket ops in helpers."""
    import ast
    import inspect

    import ultimate_coders.agent.network_mode as mod

    src = inspect.getsource(mod)
    tree = ast.parse(src)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"connect", "create_connection", "socket"}
    ]
    # Probe uses subprocess.run only; no socket.connect in planner.
    assert calls == []
