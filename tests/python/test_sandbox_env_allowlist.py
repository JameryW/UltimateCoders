"""T11 #653 (D11 #648) — sandbox env allowlist.

`SandboxManager._execute_subprocess` is the single choke point for every
agent subprocess (execute(), execute_decompose(), all adapters, streaming
and non-streaming). Before T11 it handed the FULL host environment to the
child (`env = dict(os.environ)`), so any unrelated secret in the worker's
environment leaked into every coding-agent process.

These tests pin the deny-by-default contract:

* a decoy host secret never reaches any agent subprocess (all adapters);
* agent CLIs still authenticate (allowlisted credentials pass through);
* the adapter `env_vars` overlay still adds/overrides child values but is
  not a bypass back to the unfiltered host environment;
* `UC_SANDBOX_ENV_EXTRA` widens the list and is logged at startup;
* the decompose path uses the Claude list, not the configured coding agent.

The strongest assertions run a REAL subprocess that dumps `os.environ`, so
they cover the actual `create_subprocess_exec(..., env=...)` call rather
than just the helper.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest
from ultimate_coders.agent.sandbox import (
    ADAPTER_ENV_ALLOWLIST,
    BASE_ENV_ALLOWLIST,
    ENV_EXTRA_ENV_VAR,
    GROK_AGENT_ALIASES,
    SHARED_ENV_ALLOWLIST,
    DecomposeAdapter,
    SandboxConfig,
    SandboxManager,
)

#: Host secret that must never reach a child. Not a real credential name —
#: it is exactly the kind of thing the allowlist exists to stop.
DECOY = "SECRET_TOKEN"
DECOY_VALUE = "host-secret-must-not-pass"

#: Another plausible host-side secret (cloud credentials).
DECOY_CLOUD = "AWS_SECRET_ACCESS_KEY"

#: An identity the allowlist has never heard of. Swept together with the real
#: ones so the filtering is proven *deny-by-default* ("unknown ⇒ base + shared
#: only") rather than "known ⇒ blocked".
UNKNOWN_AGENT = "some-external-plugin"

#: Every agent identity the allowlist knows about, **derived from the allowlist
#: itself** instead of hand-copied. A hand-maintained twin is exactly what let a
#: newly added adapter drift out of this sweep while the suite stayed green
#: (T13's lesson: two copies of one rule always diverge). T32 #682.
#:
#: The alias entry is kept deliberately: an alias does not hit the allowlist
#: directly, it resolves to an entry only after registry normalization.
ALL_AGENTS = [
    *ADAPTER_ENV_ALLOWLIST,
    *[alias for alias in GROK_AGENT_ALIASES if alias not in ADAPTER_ENV_ALLOWLIST],
    UNKNOWN_AGENT,
]

#: Agents a SandboxManager can actually be built for (the manager resolves
#: the adapter through the plugin registry, which raises for unknown names).
SPAWNABLE_AGENTS = [agent for agent in ALL_AGENTS if agent != UNKNOWN_AGENT]

#: agent -> a credential env var that MUST pass for that agent.
CREDENTIAL_CASES = [
    ("grok-build", "XAI_API_KEY"),
    ("claude-code", "ANTHROPIC_API_KEY"),
    ("claude-code-decompose", "ANTHROPIC_API_KEY"),
    ("codex", "OPENAI_API_KEY"),
    ("deepseek-harness", "DEEPSEEK_API_KEY"),
    ("local-harness", "OPENAI_API_KEY"),
]


def _child_env_command() -> list[str]:
    """Args for a child that prints its own environment as JSON."""
    return ["-c", "import json,os;print(json.dumps(dict(os.environ)))"]


async def _run_env_probe(
    manager: SandboxManager,
    request: dict,
    **kwargs,
) -> dict:
    """Run one subprocess through the real choke point and read its env."""
    result = await manager._execute_subprocess(request, **kwargs)
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _probe_request(command: str | None = None, **overrides) -> dict:
    request = {
        "command": command or sys.executable,
        "args": _child_env_command(),
        "timeout_secs": 30,
        "env_vars": {},
        "working_dir": tempfile.gettempdir(),
    }
    request.update(overrides)
    return request


@pytest.fixture
def decoy_host(monkeypatch):
    """Host environment carrying two unrelated secrets."""
    monkeypatch.setenv(DECOY, DECOY_VALUE)
    monkeypatch.setenv(DECOY_CLOUD, "AKIA-not-real")
    return DECOY_VALUE


# ── allowlist construction ───────────────────────────────────────


class TestAllowlistConstruction:
    """The list itself: base + shared + per-adapter + escape hatch."""

    def test_base_list_covers_posix_and_windows_core(self):
        for name in ("PATH", "HOME", "TERM", "TMPDIR", "PWD", "LANG", "LC_*"):
            assert name in BASE_ENV_ALLOWLIST
        for name in (
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "COMSPEC",
            "PATHEXT",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "PROGRAMFILES",
            "USERNAME",
            "COMPUTERNAME",
        ):
            assert name in BASE_ENV_ALLOWLIST

    def test_shared_list_covers_uc_control_plane_and_proxies(self):
        assert "UC_*" in SHARED_ENV_ALLOWLIST
        assert {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"} <= set(SHARED_ENV_ALLOWLIST)

    @pytest.mark.parametrize(
        ("agent", "credential"),
        [
            ("grok-build", "XAI_API_KEY"),
            ("grok-build", "GROK_API_KEY"),
            ("claude-code", "ANTHROPIC_API_KEY"),
            ("claude-code", "ANTHROPIC_AUTH_TOKEN"),
            ("claude-code", "ANTHROPIC_BASE_URL"),
            ("codex", "OPENAI_API_KEY"),
            ("codex", "OPENAI_BASE_URL"),
            ("codex", "OPENAI_DEFAULT_MODEL"),
            ("deepseek-harness", "DEEPSEEK_API_KEY"),
            ("local-harness", "OPENAI_API_KEY"),
        ],
    )
    def test_adapter_extensions_named_explicitly(self, agent, credential):
        assert credential in ADAPTER_ENV_ALLOWLIST[agent]

    def test_alias_normalised_to_canonical_name(self):
        config = SandboxConfig(agent="grok")
        names = config.child_env_allowlist()
        # "grok" is an alias of grok-build — must get grok's credentials.
        assert "XAI_API_KEY" in names

    def test_canonical_and_alias_agree(self):
        assert set(
            SandboxConfig(agent="grok").child_env_allowlist()
        ) == set(SandboxConfig(agent="grok-build").child_env_allowlist())

    def test_every_allowlisted_adapter_is_swept(self):
        """The sweep must stay *derived* (T32 #682).

        Regression pin: if `ALL_AGENTS` is ever turned back into a hand-copied
        list, a newly allowlisted adapter silently stops being swept -- and the
        suite stays green. This test is what makes that loud.
        """
        missing = [name for name in ADAPTER_ENV_ALLOWLIST if name not in ALL_AGENTS]
        assert not missing, (
            f"allowlist entries missing from the sweep: {missing} -- "
            "ALL_AGENTS must stay derived from ADAPTER_ENV_ALLOWLIST"
        )
        assert len(ALL_AGENTS) == len(set(ALL_AGENTS)), "duplicate identities"
        assert any(alias in ALL_AGENTS for alias in GROK_AGENT_ALIASES), (
            "the alias path (registry normalization) must stay covered"
        )
        assert UNKNOWN_AGENT not in ADAPTER_ENV_ALLOWLIST, (
            "the unknown-identity case must remain genuinely unknown"
        )

    def test_unknown_agent_gets_base_and_shared_only(self):
        names = SandboxConfig(agent=UNKNOWN_AGENT).child_env_allowlist()
        assert "PATH" in names
        assert "UC_*" in names
        for credential in ("ANTHROPIC_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY"):
            assert credential not in names

    def test_plugin_declared_key_env_is_honoured(self, monkeypatch):
        """A plugin's api_key_env is added without core-code changes."""
        import ultimate_coders.agent.registry as registry_mod

        monkeypatch.setattr(
            registry_mod, "api_key_env_for", lambda _agent: "PLUGIN_API_KEY"
        )
        names = SandboxConfig(agent=UNKNOWN_AGENT).child_env_allowlist()
        assert "PLUGIN_API_KEY" in names

    def test_explicit_agent_argument_wins_over_config(self):
        config = SandboxConfig(agent="grok-build")
        names = config.child_env_allowlist("claude-code")
        assert "ANTHROPIC_API_KEY" in names


# ── build_child_env ──────────────────────────────────────────────


class TestBuildChildEnv:
    """The filter: what survives from the host, what the overlay adds."""

    @pytest.mark.parametrize("agent", ALL_AGENTS)
    def test_decoy_host_secret_never_passes(self, agent, decoy_host):
        config = SandboxConfig(agent=agent)
        env = config.build_child_env(os.environ, {}, agent=agent)
        assert DECOY not in env
        assert DECOY_CLOUD not in env
        assert DECOY_VALUE not in env.values()

    @pytest.mark.parametrize(("agent", "credential"), CREDENTIAL_CASES)
    def test_allowlisted_credentials_pass(self, agent, credential, monkeypatch):
        monkeypatch.setenv(credential, "test-credential")
        env = SandboxConfig(agent=agent).build_child_env(
            os.environ, {}, agent=agent
        )
        assert env[credential] == "test-credential"

    def test_base_system_vars_pass(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        env = SandboxConfig(agent="grok-build").build_child_env(os.environ, {})
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["LANG"] == "en_US.UTF-8"

    def test_prefix_matching_for_lc_and_uc(self, monkeypatch):
        monkeypatch.setenv("LC_ALL", "C.UTF-8")
        monkeypatch.setenv("LC_CTYPE", "C.UTF-8")
        monkeypatch.setenv("UC_WORKER_PROJECTS", "proj-a")
        monkeypatch.setenv("LC_NOT_ALLOWED", "x")  # still LC_* → passes
        env = SandboxConfig(agent="grok-build").build_child_env(os.environ, {})
        assert env["LC_ALL"] == "C.UTF-8"
        assert env["LC_CTYPE"] == "C.UTF-8"
        assert env["UC_WORKER_PROJECTS"] == "proj-a"

    def test_matching_is_case_insensitive(self):
        """Windows-style host casing still matches the (upper-case) list."""
        host = {"Path": "C:\\Windows\\system32", "SystemRoot": "C:\\Windows"}
        env = SandboxConfig(agent="grok-build").build_child_env(host, {})
        assert env == host

    def test_a_uc_prefixed_secret_would_still_pass_by_design(self, monkeypatch):
        """UC_* is the control plane — operators own that namespace."""
        monkeypatch.setenv("UC_SOME_INTERNAL_TOKEN", "t")
        env = SandboxConfig(agent="grok-build").build_child_env(os.environ, {})
        assert env["UC_SOME_INTERNAL_TOKEN"] == "t"

    def test_overlay_adds_values(self, decoy_host):
        env = SandboxConfig(agent="grok-build").build_child_env(
            os.environ, {"GROK_HOME": "/tmp/grok-home"}
        )
        assert env["GROK_HOME"] == "/tmp/grok-home"

    def test_overlay_overrides_host_value(self, monkeypatch):
        monkeypatch.setenv("PATH", "/host/path")
        env = SandboxConfig(agent="grok-build").build_child_env(
            os.environ, {"PATH": "/sandbox/path"}
        )
        assert env["PATH"] == "/sandbox/path"

    def test_overlay_is_not_a_bypass(self, decoy_host):
        """A populated overlay must not drag the rest of the host env along."""
        env = SandboxConfig(agent="claude-code").build_child_env(
            os.environ, {"INJECTED": "1"}
        )
        assert env["INJECTED"] == "1"
        assert DECOY not in env
        assert DECOY_CLOUD not in env

    def test_overlay_credential_for_other_agent_is_not_filtered(self, decoy_host):
        """Overlay values are chosen by our own adapters — they always apply."""
        env = SandboxConfig(agent="grok-build").build_child_env(
            os.environ, {"ANTHROPIC_API_KEY": "from-overlay"}
        )
        assert env["ANTHROPIC_API_KEY"] == "from-overlay"

    def test_empty_overlay_is_fine(self):
        env = SandboxConfig(agent="grok-build").build_child_env(
            {"PATH": "/bin"}, None
        )
        assert env == {"PATH": "/bin"}

    def test_host_env_is_not_mutated(self):
        host = {"PATH": "/bin", DECOY: "x"}
        config = SandboxConfig(agent="grok-build")
        config.build_child_env(host, {"A": "1"})
        assert host == {"PATH": "/bin", DECOY: "x"}


class TestEscapeHatch:
    """UC_SANDBOX_ENV_EXTRA."""

    def test_extra_names_extend_the_list(self, monkeypatch):
        monkeypatch.setenv(ENV_EXTRA_ENV_VAR, "MY_VAR, MY_OTHER_VAR")
        names = SandboxConfig(agent="grok-build").child_env_allowlist()
        assert "MY_VAR" in names
        assert "MY_OTHER_VAR" in names

    def test_extra_names_pass_through_the_filter(self, monkeypatch):
        monkeypatch.setenv(ENV_EXTRA_ENV_VAR, "MY_VAR")
        monkeypatch.setenv("MY_VAR", "value")
        monkeypatch.setenv(DECOY, DECOY_VALUE)
        env = SandboxConfig(agent="grok-build").build_child_env(os.environ, {})
        assert env["MY_VAR"] == "value"
        assert DECOY not in env

    def test_extra_prefix_matching(self, monkeypatch):
        monkeypatch.setenv(ENV_EXTRA_ENV_VAR, "PROJ_*")
        monkeypatch.setenv("PROJ_REGION", "eu")
        monkeypatch.setenv("PROJECT_UNRELATED", "no")
        env = SandboxConfig(agent="grok-build").build_child_env(os.environ, {})
        assert env["PROJ_REGION"] == "eu"
        assert "PROJECT_UNRELATED" not in env

    def test_whitespace_and_empty_entries_ignored(self, monkeypatch):
        monkeypatch.setenv(ENV_EXTRA_ENV_VAR, " A ,,  ,B ")
        assert SandboxConfig().env_extra_names() == ("A", "B")

    def test_unset_extra_is_empty(self, monkeypatch):
        monkeypatch.delenv(ENV_EXTRA_ENV_VAR, raising=False)
        assert SandboxConfig().env_extra_names() == ()

    def test_extra_logged_at_startup(self, monkeypatch, caplog):
        monkeypatch.setenv(ENV_EXTRA_ENV_VAR, "MY_VAR,MY_OTHER_VAR")
        with caplog.at_level("INFO"):
            SandboxManager(SandboxConfig(project_path="/tmp"))
        assert any(
            ENV_EXTRA_ENV_VAR in record.message and "MY_VAR" in record.message
            for record in caplog.records
        ), caplog.text

    def test_not_logged_when_unset(self, monkeypatch, caplog):
        monkeypatch.delenv(ENV_EXTRA_ENV_VAR, raising=False)
        with caplog.at_level("INFO"):
            SandboxManager(SandboxConfig(project_path="/tmp"))
        assert not any(
            ENV_EXTRA_ENV_VAR in record.message for record in caplog.records
        ), caplog.text


# ── real subprocess (the choke point itself) ─────────────────────


class TestSubprocessEnv:
    """What the spawned child actually sees — real create_subprocess_exec."""

    @pytest.mark.parametrize("agent", SPAWNABLE_AGENTS)
    async def test_decoy_absent_from_real_subprocess(self, agent, decoy_host):
        config = SandboxConfig(agent=agent, project_path=tempfile.gettempdir())
        manager = SandboxManager(config)
        child_env = await _run_env_probe(
            manager, _probe_request(), agent=agent
        )
        assert DECOY not in child_env
        assert DECOY_CLOUD not in child_env
        assert child_env.get("PATH")

    @pytest.mark.parametrize(("agent", "credential"), CREDENTIAL_CASES)
    async def test_credential_reaches_real_subprocess(
        self, agent, credential, monkeypatch
    ):
        monkeypatch.setenv(credential, "cli-credential")
        monkeypatch.setenv(DECOY, DECOY_VALUE)
        config = SandboxConfig(agent=agent, project_path=tempfile.gettempdir())
        manager = SandboxManager(config)
        child_env = await _run_env_probe(manager, _probe_request(), agent=agent)
        assert child_env[credential] == "cli-credential"
        assert DECOY not in child_env

    async def test_agent_resolved_from_request_when_no_argument(self, decoy_host, monkeypatch):
        """request["agent"] drives the per-adapter list."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-host")
        config = SandboxConfig(agent="grok-build", project_path=tempfile.gettempdir())
        manager = SandboxManager(config)
        request = _probe_request(agent="claude-code")
        child_env = await _run_env_probe(manager, request)
        assert child_env["ANTHROPIC_API_KEY"] == "from-host"
        # ...and the configured agent's own credential is NOT added.
        assert "XAI_API_KEY" not in child_env

    async def test_request_agent_absent_falls_back_to_config(
        self, decoy_host, monkeypatch
    ):
        monkeypatch.setenv("XAI_API_KEY", "xai-host")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-host")
        config = SandboxConfig(agent="grok-build", project_path=tempfile.gettempdir())
        manager = SandboxManager(config)
        child_env = await _run_env_probe(manager, _probe_request())
        assert child_env["XAI_API_KEY"] == "xai-host"
        assert "ANTHROPIC_API_KEY" not in child_env

    async def test_overlay_reaches_child(self, decoy_host):
        config = SandboxConfig(agent="grok-build", project_path=tempfile.gettempdir())
        manager = SandboxManager(config)
        request = _probe_request(env_vars={"GROK_HOME": "/tmp/grok-home"})
        child_env = await _run_env_probe(manager, request)
        assert child_env["GROK_HOME"] == "/tmp/grok-home"

    async def test_extra_env_var_reaches_child(self, monkeypatch, decoy_host):
        monkeypatch.setenv(ENV_EXTRA_ENV_VAR, "MY_EXTRA")
        monkeypatch.setenv("MY_EXTRA", "extra-value")
        config = SandboxConfig(agent="grok-build", project_path=tempfile.gettempdir())
        manager = SandboxManager(config)
        child_env = await _run_env_probe(manager, _probe_request())
        assert child_env["MY_EXTRA"] == "extra-value"
        assert DECOY not in child_env

    async def test_streaming_path_is_filtered_too(self, decoy_host, monkeypatch):
        """The on_stdout_line branch shares the choke point — same filter."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "stream-credential")
        config = SandboxConfig(
            agent="claude-code", project_path=tempfile.gettempdir()
        )
        manager = SandboxManager(config)
        lines: list[str] = []

        async def on_line(line: str) -> None:
            lines.append(line)

        request = _probe_request()
        result = await manager._execute_subprocess(
            request, on_stdout_line=on_line, agent="claude-code"
        )
        assert result.exit_code == 0
        child_env = json.loads(lines[-1])
        assert child_env["ANTHROPIC_API_KEY"] == "stream-credential"
        assert DECOY not in child_env


# ── decompose path ───────────────────────────────────────────────


class TestDecomposePath:
    """execute_decompose() must use the Claude list, not the coding agent."""

    def test_decompose_request_carries_agent_identity(self):
        adapter = DecomposeAdapter()
        config = SandboxConfig(agent="grok-build", project_path="/tmp")
        request = adapter.build_request("Decompose", "/tmp", config)
        assert request["agent"] == "claude-code-decompose"

    async def test_decompose_subprocess_gets_anthropic_not_grok(
        self, decoy_host, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "decompose-credential")
        monkeypatch.setenv("XAI_API_KEY", "coding-agent-credential")
        config = SandboxConfig(
            agent="grok-build",  # coding agent differs from the decompose CLI
            project_path=tempfile.gettempdir(),
        )
        manager = SandboxManager(config)

        request = DecomposeAdapter().build_request(
            "Decompose", tempfile.gettempdir(), config
        )
        # The real CLI is not installed on the test host — keep the request's
        # agent/env_vars (the part under test) but run a Python probe.
        request["command"] = sys.executable
        request["args"] = _child_env_command()

        child_env = await _run_env_probe(
            manager, request, agent="claude-code-decompose"
        )
        assert child_env["ANTHROPIC_API_KEY"] == "decompose-credential"
        assert "XAI_API_KEY" not in child_env
        assert DECOY not in child_env

    async def test_decompose_without_explicit_agent_uses_request_identity(
        self, decoy_host, monkeypatch
    ):
        """The resolution order: request["agent"] covers adapter-less calls."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "decompose-credential")
        monkeypatch.setenv("XAI_API_KEY", "coding-agent-credential")
        config = SandboxConfig(
            agent="grok-build", project_path=tempfile.gettempdir()
        )
        manager = SandboxManager(config)
        request = DecomposeAdapter().build_request(
            "Decompose", tempfile.gettempdir(), config
        )
        request["command"] = sys.executable
        request["args"] = _child_env_command()

        child_env = await _run_env_probe(manager, request)
        assert child_env["ANTHROPIC_API_KEY"] == "decompose-credential"
        assert "XAI_API_KEY" not in child_env
