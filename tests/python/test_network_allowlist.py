"""T16 — default-toward-offline + LLM/API host allowlist (policy Restricted).

Unit tests only: no root / nft / unshare required. Restricted MVP backend is
``allowlist_preflight`` (policy gate, noop argv) — kernel selective egress is
deferred and must not be claimed here.

NATS/Gateway hosts are never required on the child allowlist.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from ultimate_coders.agent.network_mode import (  # noqa: E402
    ALLOWLIST_PREFLIGHT_BACKEND,
    NETWORK_ALLOWLIST_ENV,
    NETWORK_DEFAULT_MODE_ENV,
    NETWORK_DEFAULT_RESTRICTED_ENV,
    NETWORK_MODE_ENFORCE_ENV,
    NetworkApplyPlan,
    NetworkModeError,
    intended_llm_hosts,
    network_default_restricted_enabled,
    normalize_allowlist_host,
    parse_network_allowlist,
    plan_network_mode,
    plan_restricted_allowlist,
    reset_network_mode_state_for_tests,
    resolve_network_default,
    set_restricted_applicator,
)
from ultimate_coders.agent.sandbox import (  # noqa: E402
    NetworkMode,
    SandboxConfig,
    SandboxManager,
)
from ultimate_coders.agent.sandbox_dsec import (  # noqa: E402
    DSEC_DEFAULTS,
    dsec_sandbox_config,
)
from ultimate_coders.agent.worker import Worker  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_network_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(NETWORK_MODE_ENFORCE_ENV, raising=False)
    monkeypatch.delenv(NETWORK_DEFAULT_RESTRICTED_ENV, raising=False)
    monkeypatch.delenv(NETWORK_ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(NETWORK_DEFAULT_MODE_ENV, raising=False)
    for name in (
        "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "DEEPSEEK_BASE_URL",
        "MIMO_BASE_URL",
        "MEMORY_LLM_BASE_URL",
        "MEMORY_EMBEDDER_BASE_URL",
        "UC_GATEWAY_ADDR",
        "NATS_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_network_mode_state_for_tests()
    yield
    reset_network_mode_state_for_tests()


# ── default-restricted flag + resolver ───────────────────────────


def test_default_restricted_flag_defaults_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(NETWORK_DEFAULT_RESTRICTED_ENV, raising=False)
    assert network_default_restricted_enabled() is False
    assert resolve_network_default() == "full"


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_default_restricted_flag_truthy(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(NETWORK_DEFAULT_RESTRICTED_ENV, raw)
    assert network_default_restricted_enabled() is True
    assert resolve_network_default() == "restricted"


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "maybe"])
def test_default_restricted_flag_falsy(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(NETWORK_DEFAULT_RESTRICTED_ENV, raw)
    assert network_default_restricted_enabled() is False
    assert resolve_network_default() == "full"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("none", "none"),
        ("RESTRICTED", "restricted"),
        ("full", "full"),
    ],
)
def test_default_mode_env_supersedes_bool(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected: str
) -> None:
    monkeypatch.setenv(NETWORK_DEFAULT_RESTRICTED_ENV, "1")
    monkeypatch.setenv(NETWORK_DEFAULT_MODE_ENV, mode)
    assert resolve_network_default() == expected


def test_sandbox_config_literal_stays_full_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dataclass field default must not mutate when the flag is on."""
    monkeypatch.setenv(NETWORK_DEFAULT_RESTRICTED_ENV, "1")
    assert SandboxConfig().network == NetworkMode.FULL
    assert DSEC_DEFAULTS["network"] == NetworkMode.FULL


def test_dsec_helper_resolves_restricted_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NETWORK_DEFAULT_RESTRICTED_ENV, "1")
    cfg = dsec_sandbox_config(project_path="/tmp")
    assert cfg.network == "restricted"
    # Explicit override wins.
    cfg2 = dsec_sandbox_config(project_path="/tmp", network=NetworkMode.NONE)
    assert cfg2.network == NetworkMode.NONE


def test_dsec_helper_preserves_full_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(NETWORK_DEFAULT_RESTRICTED_ENV, raising=False)
    assert dsec_sandbox_config().network == NetworkMode.FULL


def test_worker_default_uses_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NETWORK_DEFAULT_RESTRICTED_ENV, "1")
    worker = Worker(worker_id="t16-test")
    assert worker._sandbox_config.network == "restricted"
    # Explicit config wins.
    worker2 = Worker(
        worker_id="t16-test-2",
        sandbox_config=SandboxConfig(network=NetworkMode.FULL),
    )
    assert worker2._sandbox_config.network == NetworkMode.FULL


# ── allowlist parse / host normalize ─────────────────────────────


def test_parse_network_allowlist_comma_space_semicolon() -> None:
    hosts = parse_network_allowlist(
        "api.anthropic.com, api.openai.com;api.deepseek.com\napi.x.ai"
    )
    assert hosts == frozenset(
        {
            "api.anthropic.com",
            "api.openai.com",
            "api.deepseek.com",
            "api.x.ai",
        }
    )


def test_normalize_allowlist_host_strips_scheme_path_port() -> None:
    assert normalize_allowlist_host("https://api.openai.com:443/v1") == "api.openai.com"
    assert normalize_allowlist_host("API.DeepSeek.COM") == "api.deepseek.com"
    assert normalize_allowlist_host("api.anthropic.com:443") == "api.anthropic.com"


def test_intended_llm_hosts_from_base_url_envs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    # Control-plane URLs must not appear in the intended set.
    monkeypatch.setenv("UC_GATEWAY_ADDR", "gateway.internal:50051")
    monkeypatch.setenv("NATS_URL", "nats://nats.internal:4222")
    hosts = intended_llm_hosts()
    assert hosts == frozenset({"api.anthropic.com", "api.deepseek.com"})
    assert "gateway.internal" not in hosts
    assert "nats.internal" not in hosts


# ── Restricted preflight applicator ──────────────────────────────


def test_empty_allowlist_fail_closed() -> None:
    with pytest.raises(NetworkModeError, match="empty"):
        plan_restricted_allowlist("restricted", allowlist=frozenset())


def test_allowlisted_intended_host_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NETWORK_ALLOWLIST_ENV, "api.anthropic.com,api.openai.com")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    plan = plan_restricted_allowlist("restricted")
    assert plan == NetworkApplyPlan(
        mode="restricted",
        argv_prefix=(),
        backend=ALLOWLIST_PREFLIGHT_BACKEND,
    )


def test_non_allowlisted_intended_host_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NETWORK_ALLOWLIST_ENV, "api.openai.com")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    with pytest.raises(NetworkModeError, match="api.anthropic.com"):
        plan_restricted_allowlist("restricted")


def test_extra_allowlist_entries_ok_without_intended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        NETWORK_ALLOWLIST_ENV,
        "api.anthropic.com,api.openai.com,api.deepseek.com",
    )
    plan = plan_restricted_allowlist("restricted")
    assert plan.backend == ALLOWLIST_PREFLIGHT_BACKEND
    assert plan.argv_prefix == ()


def test_nats_gateway_not_required_for_spawn_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker control-plane hosts are not on the child allowlist requirement."""
    monkeypatch.setenv(NETWORK_ALLOWLIST_ENV, "api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("UC_GATEWAY_ADDR", "gw.example:50051")
    monkeypatch.setenv("NATS_URL", "nats://nats.example:4222")
    # Succeeds without listing gateway/nats hosts.
    plan = plan_restricted_allowlist("restricted")
    assert plan.backend == ALLOWLIST_PREFLIGHT_BACKEND


def test_plan_network_mode_uses_registered_allowlist_applicator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NETWORK_ALLOWLIST_ENV, "api.openai.com")
    set_restricted_applicator(plan_restricted_allowlist)
    plan = plan_network_mode("restricted", enforce=True)
    assert plan.backend == ALLOWLIST_PREFLIGHT_BACKEND
    assert plan.argv_prefix == ()


def test_sandbox_manager_registers_restricted_applicator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")
    monkeypatch.setenv(NETWORK_ALLOWLIST_ENV, "api.openai.com")
    # Creating a manager registers the product applicator.
    SandboxManager(SandboxConfig(project_path=tempfile.gettempdir()))
    plan = plan_network_mode("restricted", enforce=True)
    assert plan.backend == ALLOWLIST_PREFLIGHT_BACKEND


@pytest.mark.asyncio
async def test_subprocess_restricted_empty_allowlist_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")
    monkeypatch.delenv(NETWORK_ALLOWLIST_ENV, raising=False)
    manager = SandboxManager(
        SandboxConfig(
            project_path=tempfile.gettempdir(),
            network=NetworkMode.RESTRICTED,
        )
    )
    result = await manager._execute_subprocess(
        {
            "command": sys.executable,
            "args": ["-c", "print('should-not-run')"],
            "timeout_secs": 30,
            "env_vars": {},
            "working_dir": tempfile.gettempdir(),
        }
    )
    assert result.exit_code == -1
    assert "allowlist" in result.stderr.lower() or "empty" in result.stderr.lower()
    assert "should-not-run" not in result.stdout


@pytest.mark.asyncio
async def test_subprocess_restricted_allowlist_allow_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")
    monkeypatch.setenv(NETWORK_ALLOWLIST_ENV, "api.openai.com")
    manager = SandboxManager(
        SandboxConfig(
            project_path=tempfile.gettempdir(),
            network=NetworkMode.RESTRICTED,
        )
    )
    result = await manager._execute_subprocess(
        {
            "command": sys.executable,
            "args": ["-c", "print('allowed')"],
            "timeout_secs": 30,
            "env_vars": {},
            "working_dir": tempfile.gettempdir(),
        }
    )
    assert result.exit_code == 0
    assert "allowed" in result.stdout


@pytest.mark.asyncio
async def test_subprocess_restricted_deny_non_allowlisted_intended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NETWORK_MODE_ENFORCE_ENV, "1")
    monkeypatch.setenv(NETWORK_ALLOWLIST_ENV, "api.openai.com")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    manager = SandboxManager(
        SandboxConfig(
            project_path=tempfile.gettempdir(),
            network=NetworkMode.RESTRICTED,
        )
    )
    result = await manager._execute_subprocess(
        {
            "command": sys.executable,
            "args": ["-c", "print('denied')"],
            "timeout_secs": 30,
            "env_vars": {},
            "working_dir": tempfile.gettempdir(),
        }
    )
    assert result.exit_code == -1
    assert "api.anthropic.com" in result.stderr
    assert "denied" not in result.stdout


def test_flag_off_preserves_enforce_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T15 regression: enforce off → noop even for restricted."""
    monkeypatch.delenv(NETWORK_MODE_ENFORCE_ENV, raising=False)
    monkeypatch.setenv(NETWORK_DEFAULT_RESTRICTED_ENV, "1")
    set_restricted_applicator(plan_restricted_allowlist)
    plan = plan_network_mode("restricted", enforce=False)
    assert plan.backend == "noop"
    assert plan.argv_prefix == ()
