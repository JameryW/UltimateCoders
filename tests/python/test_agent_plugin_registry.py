"""Tests for the agent adapter plugin registry and the DeepSeek harness.

Covers:
- registry: built-in registration, aliases, unknown-name errors, idempotent
  re-registration, duplicate conflict detection
- plugin factory delegation from sandbox.create_adapter / available_agents
- per-agent API key env mapping (SandboxConfig._build_env_vars)
- DeepSeek Harness adapter (official `dsh` CLI): request building
  (dsh --profile headless "<task>"), env credential forwarding,
  parse_output for the headless stdout/exit-code contract
- external plugin discovery via UC_AGENT_PLUGINS
- capability advertising driven by the registry
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from ultimate_coders.agent.harness_deepseek import DeepSeekHarnessAdapter
from ultimate_coders.agent.registry import (
    AgentAdapterRegistry,
    AgentPluginSpec,
    discover_plugins,
    ensure_builtin_plugins,
    registry,
)
from ultimate_coders.agent.sandbox import (
    ExecResult,
    SandboxConfig,
    available_agents,
    create_adapter,
)

# ── Registry basics ────────────────────────────────────────────


class TestRegistry:
    def test_builtins_registered(self) -> None:
        ensure_builtin_plugins()
        agents = available_agents()
        for expected in (
            "grok-build", "grok",
            "claude-code", "claude-code-decompose", "codex",
            "deepseek-harness", "deepseek",
        ):
            assert expected in agents, f"{expected} missing from {agents}"
        # no duplicate entries
        assert len(agents) == len(set(agents))

    def test_alias_resolves_same_spec(self) -> None:
        ensure_builtin_plugins()
        canonical = registry.get_spec("grok-build")
        alias = registry.get_spec("grok")
        assert canonical is not None and alias is not None
        assert canonical is alias
        assert registry.get_spec("deepseek") is registry.get_spec("deepseek-harness")

    def test_unknown_agent_raises_with_available_list(self) -> None:
        with pytest.raises(ValueError, match="Unknown agent.*deepseek-harness"):
            create_adapter("no-such-agent")

    def test_builtin_registration_is_idempotent(self) -> None:
        ensure_builtin_plugins()
        ensure_builtin_plugins()  # second call must not raise
        assert "grok-build" in available_agents()

    def test_conflicting_registration_is_rejected(self) -> None:
        reg = AgentAdapterRegistry()
        sentinel_a = object()
        sentinel_b = object()
        reg.register(AgentPluginSpec(name="x", factory=lambda: sentinel_a))
        with pytest.raises(ValueError, match="already registered"):
            reg.register(AgentPluginSpec(name="x", factory=lambda: sentinel_b))
        # replace=True allows explicit overrides
        reg.register(AgentPluginSpec(name="x", factory=lambda: sentinel_b), replace=True)
        assert reg.create("x") is sentinel_b

    def test_api_key_env_lookup(self) -> None:
        from ultimate_coders.agent.registry import api_key_env_for

        assert api_key_env_for("deepseek-harness") == "DEEPSEEK_API_KEY"
        assert api_key_env_for("deepseek") == "DEEPSEEK_API_KEY"  # alias
        assert api_key_env_for("grok-build") == "XAI_API_KEY"
        assert api_key_env_for("claude-code") == "ANTHROPIC_API_KEY"
        assert api_key_env_for("codex") == "OPENAI_API_KEY"
        assert api_key_env_for("unknown-agent") is None


class TestSandboxFactoryDelegation:
    def test_create_adapter_types(self) -> None:
        assert type(create_adapter("grok-build")).__name__ == "GrokBuildAdapter"
        assert type(create_adapter("grok")).__name__ == "GrokBuildAdapter"
        assert type(create_adapter("claude-code")).__name__ == "ClaudeCodeAdapter"
        assert type(create_adapter("codex")).__name__ == "CodexAdapter"
        assert type(create_adapter("deepseek-harness")).__name__ == "DeepSeekHarnessAdapter"

    def test_api_key_env_mapping_via_config(self) -> None:
        cfg = SandboxConfig(agent="deepseek-harness", api_key="sk-test")
        env = cfg._build_env_vars()
        assert env.get("DEEPSEEK_API_KEY") == "sk-test"

        cfg = SandboxConfig(agent="grok-build", api_key="xai-test")
        assert cfg._build_env_vars().get("XAI_API_KEY") == "xai-test"

        cfg = SandboxConfig(agent="claude-code", api_key="anthropic-test")
        assert cfg._build_env_vars().get("ANTHROPIC_API_KEY") == "anthropic-test"


# ── External plugin discovery (UC_AGENT_PLUGINS) ────────────────


class TestPluginDiscovery:
    def test_env_path_plugin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plugin = tmp_path / "my_probe_agent.py"
        plugin.write_text(
            "from ultimate_coders.agent.registry import AgentPluginSpec, register_agent\n"
            "from ultimate_coders.agent.sandbox import AgentAdapter, AgentOutput\n"
            "class ProbeAdapter(AgentAdapter):\n"
            "    def name(self): return 'probe-agent'\n"
            "    def build_request(self, prompt, working_dir, config, subtask_config=None):\n"
            "        return {'command': 'probe', 'args': []}\n"
            "    def parse_output(self, result): return AgentOutput(summary='probe')\n"
            "register_agent(AgentPluginSpec(name='probe-agent', factory=ProbeAdapter,\n"
            "    api_key_env='PROBE_KEY', cli_probe='probe'))\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("UC_AGENT_PLUGINS", str(plugin))
        try:
            discover_plugins()
            spec = registry.get_spec("probe-agent")
            assert spec is not None and spec.api_key_env == "PROBE_KEY"
            adapter = create_adapter("probe-agent")
            assert adapter.name() == "probe-agent"
            # CLI-backed external plugin appears in available_agents only via
            # the registry listing (no PATH probe there)
            assert "probe-agent" in registry.available()
        finally:
            registry._specs.pop("probe-agent", None)

    def test_missing_plugin_path_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UC_AGENT_PLUGINS", str(tmp_path / "does-not-exist.py"))
        before = set(registry.available())
        discover_plugins()  # must not raise
        assert set(registry.available()) >= before

    def test_double_discovery_does_not_reexecute_or_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Direct discover_plugins() + create_adapter()'s discover_once()
        must import the plugin file only once — re-execution rebuilds the
        classes, trips the duplicate-registration guard, and warns."""
        plugin = tmp_path / "twice_probe_agent.py"
        plugin.write_text(
            "from ultimate_coders.agent.registry import AgentPluginSpec, register_agent\n"
            "from ultimate_coders.agent.sandbox import AgentAdapter, AgentOutput\n"
            "class TwiceAdapter(AgentAdapter):\n"
            "    def name(self): return 'twice-agent'\n"
            "    def build_request(self, prompt, working_dir, config, subtask_config=None):\n"
            "        return {'command': 'true', 'args': []}\n"
            "    def parse_output(self, result): return AgentOutput(summary='twice')\n"
            "register_agent(AgentPluginSpec(name='twice-agent', factory=TwiceAdapter))\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("UC_AGENT_PLUGINS", str(plugin))
        try:
            import logging as _logging

            with caplog.at_level(_logging.WARNING, logger="ultimate_coders.agent.registry"):
                discover_plugins()          # first discovery: imports + registers
                first = registry.get_spec("twice-agent")
                create_adapter("twice-agent")  # internal discover_once() must no-op
                discover_plugins()          # direct repeat must also no-op
            conflicts = [
                r for r in caplog.records if "already registered" in r.getMessage()
            ]
            assert not conflicts, f"spurious duplicate warnings: {conflicts}"
            assert registry.get_spec("twice-agent") is first  # same spec object
        finally:
            registry._specs.pop("twice-agent", None)


# ── DeepSeek Harness adapter (official dsh CLI) ─────────────────


class TestDeepSeekAdapterRequest:
    def test_build_request_headless_contract(self, tmp_path: Path) -> None:
        cfg = SandboxConfig(agent="deepseek-harness", project_path=str(tmp_path))
        request = DeepSeekHarnessAdapter().build_request("fix the failing test", str(tmp_path), cfg)

        assert request["command"] == "dsh"
        # dsh --profile headless "<task>" — task is the sole positional
        assert request["args"][:2] == ["--profile", "headless"]
        assert request["args"][2] == "fix the failing test"
        assert request["working_dir"] == str(tmp_path)
        assert request["timeout_secs"] == cfg.max_cpu_seconds

    def test_build_request_injects_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
        cfg = SandboxConfig(
            agent="deepseek-harness", project_path=str(tmp_path), api_key="sk-ds"
        )
        request = DeepSeekHarnessAdapter().build_request("p", str(tmp_path), cfg)
        assert request["env_vars"].get("DEEPSEEK_API_KEY") == "sk-ds"
        # No explicit gateway → do not override dsh's public default
        assert "DEEPSEEK_BASE_URL" not in request["env_vars"]

    def test_build_request_forwards_base_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://gw.internal/v1")
        cfg = SandboxConfig(agent="deepseek-harness", project_path=str(tmp_path))
        request = DeepSeekHarnessAdapter().build_request("p", str(tmp_path), cfg)
        assert request["env_vars"].get("DEEPSEEK_BASE_URL") == "https://gw.internal/v1"

    def test_subtask_config_profile_override(self, tmp_path: Path) -> None:
        cfg = SandboxConfig(agent="deepseek-harness", project_path=str(tmp_path))
        request = DeepSeekHarnessAdapter().build_request(
            "p",
            str(tmp_path),
            cfg,
            subtask_config={"dsh_profile": "headless-review"},
        )
        assert request["args"][:2] == ["--profile", "headless-review"]


class TestDeepSeekAdapterParse:
    def _result(self, stdout: str = "", stderr: str = "", exit_code: int = 0):
        return ExecResult(stdout=stdout, stderr=stderr, exit_code=exit_code)

    def test_parse_success_contract(self) -> None:
        # Headless contract: final assistant text on stdout, exit 0.
        out = DeepSeekHarnessAdapter().parse_output(
            self._result("Fixed the test and added regression coverage.\n", exit_code=0)
        )
        assert out.success is True
        assert out.summary == "Fixed the test and added regression coverage."

    def test_parse_failure_contract(self) -> None:
        out = DeepSeekHarnessAdapter().parse_output(
            self._result("", stderr="model error: 401 unauthorized", exit_code=1)
        )
        assert out.success is False
        assert "exited with code 1" in out.summary
        assert "401 unauthorized" in out.stderr_tail

    def test_parse_timeout(self) -> None:
        result = ExecResult(stdout="", stderr="slow", exit_code=-1, timed_out=True)
        out = DeepSeekHarnessAdapter().parse_output(result)
        assert out.success is False
        assert "timed out" in out.summary.lower()

    def test_parse_truncates_very_long_stdout(self) -> None:
        out = DeepSeekHarnessAdapter().parse_output(self._result("x" * 5000))
        assert out.success is True
        assert len(out.summary) <= 2001
        assert out.summary.endswith("…")


# ── Capability advertising ──────────────────────────────────────


class TestCapabilityAdvertising:
    def test_registry_capability_names(self) -> None:
        ensure_builtin_plugins()
        # All built-ins are CLI-backed: nothing on PATH → nothing advertised
        names = registry.capability_names(lambda exe: False)
        assert "grok-build" not in names
        assert "deepseek-harness" not in names
        assert names == []

        # dsh present → deepseek-harness + deepseek alias advertised
        names = registry.capability_names(lambda exe: exe == "dsh")
        assert "deepseek-harness" in names
        assert "deepseek" in names
        assert "grok-build" not in names

        # grok present → grok-build + grok alias advertised
        def probe(exe: str) -> bool:
            return exe == "grok"

        names = registry.capability_names(probe)
        assert "grok-build" in names and "grok" in names
        assert "claude-code" not in names
        # internal adapter never advertised
        assert "claude-code-decompose" not in names

    def test_worker_derive_capabilities_plugin_driven(self) -> None:
        from ultimate_coders.agent.worker import Worker

        stub = SimpleNamespace(_sandbox_config=SandboxConfig(agent="deepseek-harness"))
        caps = Worker._derive_capabilities(stub)
        assert "code" in caps  # base capability untouched
        if shutil.which("grok"):
            assert "grok-build" in caps
        if shutil.which("dsh"):
            assert "deepseek-harness" in caps
            assert "deepseek" in caps  # alias advertised alongside
