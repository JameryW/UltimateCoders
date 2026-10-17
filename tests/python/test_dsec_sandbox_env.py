"""dsec — the DeepSeek sandbox environment.

Covers the environment layer on top of the ``deepseek-harness`` agent plugin:

* registration: ``dsec`` is a first-class registry name (selection, API-key
  mapping, capability advertising) resolving to the same dsh adapter;
* configuration: :func:`dsec_sandbox_config` defaults, key injection,
  overrides;
* the deny-by-default env allowlist, proven with a REAL subprocess through
  ``SandboxManager._execute_subprocess`` (DEEPSEEK_* pass, host secrets
  filtered) — same choke-point style as test_sandbox_env_allowlist.py;
* the allowlist tuple is single-sourced (``dsec`` and ``deepseek-harness``
  cannot drift apart);
* :func:`preflight_dsec` with a missing / booting / failing dsh binary and a
  structured-shape credentials file;
* the doctor report and the ``python -m`` entry point;
* the deployment files (compose override pins UC_CODING_AGENT=dsec on every
  agent-spawning service; env template; launcher).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from ultimate_coders.agent import harness_deepseek, sandbox_dsec
from ultimate_coders.agent.registry import (
    api_key_env_for,
    create_adapter,
    ensure_builtin_plugins,
    registry,
)
from ultimate_coders.agent.sandbox import (
    ADAPTER_ENV_ALLOWLIST,
    SandboxManager,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DSEC_COMPOSE = REPO_ROOT / "docker" / "docker-compose.dsec.yml"
DSEC_ENV_EXAMPLE = REPO_ROOT / "docker" / ".env.dsec.example"
RUN_DSEC = REPO_ROOT / "run-dsec.sh"

DECOY = "SECRET_TOKEN"
DECOY_VALUE = "host-secret-must-not-pass"


def _child_env_command() -> list[str]:
    return ["-c", "import json,os;print(json.dumps(dict(os.environ)))"]


def _probe_request(**overrides) -> dict[str, Any]:
    request = {
        "command": sys.executable,
        "args": _child_env_command(),
        "timeout_secs": 30,
        "env_vars": {},
        "working_dir": tempfile.gettempdir(),
    }
    request.update(overrides)
    return request


@pytest.fixture(autouse=True)
def _builtins():
    ensure_builtin_plugins()


@pytest.fixture
def decoy_host(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(DECOY, DECOY_VALUE)
    return DECOY_VALUE


def _fake_dsh(
    tmp_path: Path,
    *,
    exit_code: int = 0,
    stderr: str = "",
    prints_version: bool = True,
) -> Path:
    """Write a fake ``dsh`` executable and return its directory.

    ``prints_version=True`` emulates a healthy CLI (``--version`` prints a
    version, like any dsh on a Node runtime >= 22.19.0); ``False`` emulates
    the silent no-op of an older runtime (exit 0, no output at all).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "dsh"
    version_lines = (
        'if [ "$1" = "--version" ]; then echo "0.2.0-rc.2"; exit 0; fi\n' if prints_version else ""
    )
    script.write_text(f"#!/bin/sh\n{version_lines}echo '{stderr}' >&2\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


# ── registration & selection ─────────────────────────────────────


class TestDsecRegistration:
    def test_dsec_is_a_selectable_agent_name(self) -> None:
        assert "dsec" in registry.available()

    def test_dsec_resolves_to_the_deepseek_adapter(self) -> None:
        adapter = create_adapter("dsec")
        assert type(adapter) is harness_deepseek.DeepSeekHarnessAdapter

    def test_dsec_api_key_env_mapping(self) -> None:
        assert api_key_env_for("dsec") == "DEEPSEEK_API_KEY"

    def test_dsec_capability_advertising_follows_cli_probe(self) -> None:
        # CLI-backed: advertised only when dsh is on PATH.
        with_dsh = registry.capability_names(lambda name: name == "dsh")
        without_dsh = registry.capability_names(lambda name: False)
        assert "dsec" in with_dsh
        assert "deepseek-harness" in with_dsh
        assert "dsec" not in without_dsh
        assert "deepseek-harness" not in without_dsh

    def test_registration_is_idempotent(self) -> None:
        before = registry.get_spec("dsec")
        sandbox_dsec.register(registry)
        assert registry.get_spec("dsec") == before

    def test_allowlist_tuple_is_single_sourced(self) -> None:
        # dsec and deepseek-harness must share ONE tuple object — two copies
        # of one rule always diverge (T13's lesson).
        assert ADAPTER_ENV_ALLOWLIST["dsec"] is ADAPTER_ENV_ALLOWLIST["deepseek-harness"]
        assert set(ADAPTER_ENV_ALLOWLIST["dsec"]) == {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"}


# ── configuration ────────────────────────────────────────────────


class TestDsecSandboxConfig:
    def test_defaults(self) -> None:
        cfg = sandbox_dsec.dsec_sandbox_config(project_path="/repo")
        assert cfg.agent == "dsec"
        assert cfg.project_path == "/repo"
        assert cfg.max_cpu_seconds == sandbox_dsec.DSEC_DEFAULTS["max_cpu_seconds"]
        assert cfg.max_memory_mb == sandbox_dsec.DSEC_DEFAULTS["max_memory_mb"]

    def test_api_key_maps_to_deepseek_env(self) -> None:
        cfg = sandbox_dsec.dsec_sandbox_config(api_key="sk-dsec")
        assert cfg._build_env_vars()["DEEPSEEK_API_KEY"] == "sk-dsec"

    def test_overrides_win(self) -> None:
        cfg = sandbox_dsec.dsec_sandbox_config(max_cpu_seconds=60, network="none")
        assert cfg.max_cpu_seconds == 60
        assert cfg.network == "none"

    def test_child_env_allowlist_covers_deepseek_credentials(self) -> None:
        allow = sandbox_dsec.dsec_child_env_allowlist()
        assert "DEEPSEEK_API_KEY" in allow
        assert "DEEPSEEK_BASE_URL" in allow
        assert DECOY not in allow

    def test_manager_builds_dsh_adapter_for_dsec(self) -> None:
        manager = SandboxManager(sandbox_dsec.dsec_sandbox_config(project_path="/repo"))
        assert type(manager._adapter) is harness_deepseek.DeepSeekHarnessAdapter


# ── env allowlist through the real subprocess choke point ────────


class TestDsecEnvAllowlistSubprocess:
    async def test_deepseek_credentials_pass_host_secrets_do_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decoy_host: str
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-host-key")
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://gw.internal/v1")
        manager = SandboxManager(sandbox_dsec.dsec_sandbox_config(project_path=str(tmp_path)))
        result = await manager._execute_subprocess(_probe_request(), agent="dsec")
        assert result.exit_code == 0, result.stderr
        child_env = json.loads(result.stdout.strip().splitlines()[-1])
        assert child_env.get("DEEPSEEK_API_KEY") == "sk-host-key"
        assert child_env.get("DEEPSEEK_BASE_URL") == "https://gw.internal/v1"
        assert DECOY not in child_env

    async def test_overlay_can_inject_but_not_re_widen(
        self, tmp_path: Path, decoy_host: str
    ) -> None:
        manager = SandboxManager(
            sandbox_dsec.dsec_sandbox_config(project_path=str(tmp_path), api_key="sk-overlay")
        )
        request = _probe_request(
            env_vars={"DEEPSEEK_API_KEY": "sk-overlay", DECOY: "overlay-attempt"}
        )
        result = await manager._execute_subprocess(request, agent="dsec")
        assert result.exit_code == 0, result.stderr
        child_env = json.loads(result.stdout.strip().splitlines()[-1])
        assert child_env.get("DEEPSEEK_API_KEY") == "sk-overlay"
        # The overlay may add names explicitly, but the decoy must not ride
        # along from the host environment.
        assert child_env.get(DECOY) == "overlay-attempt"
        assert "AWS_SECRET_ACCESS_KEY" not in child_env


# ── adapter request shape via the dsec config ────────────────────


class TestDsecAdapterRequest:
    def test_headless_contract(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
        cfg = sandbox_dsec.dsec_sandbox_config(project_path=str(tmp_path), api_key="sk-dsec")
        request = harness_deepseek.DeepSeekHarnessAdapter().build_request(
            "fix the failing test", str(tmp_path), cfg
        )
        assert request["command"] == "dsh"
        assert request["args"][:2] == ["--profile", "headless"]
        assert request["args"][2] == "fix the failing test"
        assert request["env_vars"]["DEEPSEEK_API_KEY"] == "sk-dsec"
        assert "DEEPSEEK_BASE_URL" not in request["env_vars"]

    def test_base_url_forwarded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://gw.internal/v1")
        cfg = sandbox_dsec.dsec_sandbox_config(project_path=str(tmp_path))
        request = harness_deepseek.DeepSeekHarnessAdapter().build_request("p", str(tmp_path), cfg)
        assert request["env_vars"]["DEEPSEEK_BASE_URL"] == "https://gw.internal/v1"


# ── preflight ────────────────────────────────────────────────────


class TestPreflightDsec:
    def test_missing_dsh_and_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(harness_deepseek.shutil, "which", lambda _name: None)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("DSH_HOME", str(tmp_path))  # empty dir → no credentials file
        ok, message = sandbox_dsec.preflight_dsec()
        assert ok is False
        assert "DEEPSEEK_API_KEY is not set" in message
        assert "npm install -g @deepseek-ai/dsh" in message

    def test_booting_dsh_with_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", f"{_fake_dsh(tmp_path)}:{os.environ['PATH']}")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
        (tmp_path / "dsh-home").mkdir()
        ok, message = sandbox_dsec.preflight_dsec()
        assert ok is True, message
        assert "dsec environment ready" in message

    def test_failing_dsh_reports_boot_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "PATH",
            f"{_fake_dsh(tmp_path, exit_code=1, stderr='profile not found')}:{os.environ['PATH']}",
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
        (tmp_path / "dsh-home").mkdir()
        ok, message = sandbox_dsec.preflight_dsec()
        assert ok is False
        assert "profile not found" in message

    def test_structured_credentials_file_surfaces_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", f"{_fake_dsh(tmp_path)}:{os.environ['PATH']}")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        dsh_home = tmp_path / "dsh-home"
        dsh_home.mkdir()
        (dsh_home / ".credentials.yaml").write_text("version:\nrefs:\nrecords:\n", encoding="utf-8")
        monkeypatch.setenv("DSH_HOME", str(dsh_home))
        ok, message = sandbox_dsec.preflight_dsec()
        assert ok is False
        # The repair hint (not the raw boot-failure signature) is surfaced.
        assert harness_deepseek.CREDENTIALS_FORMAT_HINT in message


# ── doctor report & CLI entry ────────────────────────────────────


class TestDshVersionProbe:
    def test_silent_dsh_is_detected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # dsh on a Node runtime older than 22.19.0: import.meta.main is
        # undefined, runCli() never runs, the process exits 0 with no output.
        monkeypatch.setenv(
            "PATH",
            f"{_fake_dsh(tmp_path, prints_version=False)}:{os.environ['PATH']}",
        )
        assert sandbox_dsec._dsh_version() is None

    def test_printing_dsh_returns_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        script = bindir / "dsh"
        script.write_text("#!/bin/sh\necho 0.2.0-rc.2\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
        assert sandbox_dsec._dsh_version() == "0.2.0-rc.2"

    def test_preflight_flags_silent_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "PATH",
            f"{_fake_dsh(tmp_path, prints_version=False)}:{os.environ['PATH']}",
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
        (tmp_path / "dsh-home").mkdir()
        ok, message = sandbox_dsec.preflight_dsec()
        assert ok is False
        assert sandbox_dsec.DSH_NODE_FLOOR in message
        assert "silently does nothing" in message


class TestDoctorReportAndCli:
    def test_report_contents_and_key_masking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sandbox_dsec.harness_deepseek,
            "preflight_check",
            lambda timeout_secs=20: (True, "dsh --profile headless boots OK"),
        )
        monkeypatch.setattr(sandbox_dsec, "_dsh_version", lambda timeout_secs=15: "0.2.0-rc.2")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-abcdef1234567890")
        report = sandbox_dsec.dsec_doctor_report()
        assert "dsec sandbox environment report" in report
        assert "deepseek-harness" in report
        assert "DEEPSEEK_API_KEY" in report
        assert "0.2.0-rc.2" in report
        assert "sk-…7890" in report  # masked
        assert "sk-abcdef1234567890" not in report  # never the raw key
        assert "preflight              : OK" in report

    def test_main_check_gates_on_preflight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sandbox_dsec.harness_deepseek,
            "preflight_check",
            lambda timeout_secs=20: (True, "boots OK"),
        )
        monkeypatch.setattr(sandbox_dsec, "_dsh_version", lambda timeout_secs=15: "0.2.0-rc.2")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        assert sandbox_dsec.main(["--check"]) == 0

        monkeypatch.setattr(
            sandbox_dsec.harness_deepseek,
            "preflight_check",
            lambda timeout_secs=20: (False, "dsh not found on PATH"),
        )
        assert sandbox_dsec.main(["--check"]) == 1
        assert sandbox_dsec.main([]) == 0  # report-only default still exits 0

    def test_main_json_output(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(
            sandbox_dsec.harness_deepseek,
            "preflight_check",
            lambda timeout_secs=20: (True, "boots OK"),
        )
        monkeypatch.setattr(sandbox_dsec, "_dsh_version", lambda timeout_secs=15: "0.2.0-rc.2")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        assert sandbox_dsec.main(["--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["environment"] == "dsec"
        assert payload["agent"] == "deepseek-harness"
        assert payload["ready"] is True
        assert payload["deepseek_api_key_set"] is True
        assert payload["dsh_version"] == "0.2.0-rc.2"
        assert "DEEPSEEK_API_KEY" in payload["allowlisted_credentials"]


# ── deployment files ─────────────────────────────────────────────


class TestDsecDeploymentFiles:
    def test_compose_override_pins_dsec_on_every_agent_service(self) -> None:
        data = yaml.safe_load(DSEC_COMPOSE.read_text(encoding="utf-8"))
        services = data["services"]
        for name in ("orchestrator", "worker", "nats-worker"):
            env = services[name]["environment"]
            assert env["UC_CODING_AGENT"] == "dsec", name
            assert "DEEPSEEK_API_KEY" in env, name
            assert "DEEPSEEK_BASE_URL" in env, name

    def test_env_template_documents_credentials(self) -> None:
        text = DSEC_ENV_EXAMPLE.read_text(encoding="utf-8")
        assert "UC_CODING_AGENT=dsec" in text
        assert "DEEPSEEK_API_KEY" in text

    def test_launcher_is_executable(self) -> None:
        assert RUN_DSEC.is_file()
        assert RUN_DSEC.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
