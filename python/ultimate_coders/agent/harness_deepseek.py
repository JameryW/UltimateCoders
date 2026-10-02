"""DeepSeek Harness (``dsh``) agent adapter — in-tree plugin.

Integrates the official DeepSeek Harness CLI
(https://github.com/deepseek-ai/deepseek-harness) as a coding agent for
the Orchestrator sandbox layer. Unlike the earlier experimental in-process
API loop, this adapter shells out to ``dsh`` exactly like the grok /
claude-code / codex CLI adapters.

Headless contract (per the upstream architecture notes):

    dsh --profile headless "<task>"

- one non-blank task positional, executed in the CURRENT working
  directory (the sandbox's worktree)
- final assistant text + newline on stdout
- exit 0 exactly when the turn completed; errors on stderr with exit 1
- no listening port, one fresh persisted session per invocation

Credentials (no Web UI setup needed in containers):
    DEEPSEEK_API_KEY   — required for real runs
    DEEPSEEK_BASE_URL  — optional; defaults to the public DeepSeek API

Selection:
    UC_CODING_AGENT=deepseek-harness   (aliases: deepseek)

Install (already in the worker Dockerfile):
    npm install -g @deepseek-ai/dsh    # provides the `dsh` binary

Boot diagnosis (no API cost):
    from ultimate_coders.agent.harness_deepseek import preflight_check
    ok, message = preflight_check()  # dsh --profile headless --dump-config
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ultimate_coders.agent.sandbox import AgentAdapter, AgentOutput, ExecResult, SandboxConfig

AGENT_NAME = "deepseek-harness"
AGENT_ALIASES = ("deepseek",)
DEFAULT_PROFILE = "headless"

#: Substring of the dsh boot failure emitted by dsh 0.1.0-rc.x when
#: ``$DSH_HOME/.credentials.yaml`` is in the structured
#: (``version:/refs:/records:``) shape while that build only accepts a flat
#: string-to-string map. The top-level ``version: 1`` parses as an int,
#: hence "must be a string". dsh >= 0.2.0-rc.2 (the pinned version) accepts
#: both shapes, so this signature means the installed dsh is outdated.
CREDENTIALS_FORMAT_SIGNATURE = 'must be a string'

CREDENTIALS_FORMAT_HINT = (
    "dsh cannot boot: $DSH_HOME/.credentials.yaml is in the structured "
    "(version:/refs:/records:) shape but the installed dsh build only accepts "
    "a flat KEY: value string map. Preferred fix: upgrade dsh to the pinned "
    "version (npm install -g @deepseek-ai/dsh@0.2.0-rc.2), which accepts both "
    "shapes. Alternative for a pinned old dsh: back the file up, then flatten "
    "just the refs mapping to top-level keys (e.g. DEEPSEEK_API_KEY: sk-...). "
    "Verify with: dsh --profile headless --dump-config"
)


def _dsh_home() -> Path:
    """Harness home dir (honours DSH_HOME like dsh itself)."""
    override = os.environ.get("DSH_HOME")
    if override:
        return Path(override)
    return Path.home() / ".dsh"


def credentials_format_hint(dsh_home: Path | None = None) -> str | None:
    """Return the repair hint when the credentials file has the structured shape.

    Lightweight line-based check (no yaml dependency): a flat credentials
    document never has ``version:``/``refs:``/``records:`` at column 0.
    Returns None when the file is absent (nothing to diagnose) or looks flat.
    """
    creds = (_dsh_home() if dsh_home is None else dsh_home) / ".credentials.yaml"
    try:
        text = creds.read_text(encoding="utf-8")
    except OSError:
        return None
    structured_markers = {"version:", "refs:", "records:"}
    for line in text.splitlines():
        if line.split("#", 1)[0].strip() in structured_markers and not line.startswith((" ", "\t")):
            return CREDENTIALS_FORMAT_HINT
    return None


def preflight_check(
    profile: str = DEFAULT_PROFILE,
    timeout_secs: int = 20,
) -> tuple[bool, str]:
    """Validate that ``dsh --profile <profile>`` can boot (no API cost).

    Runs ``dsh --profile <profile> --dump-config``, which exercises the full
    plugin/credentials load path without dispatching a task. Returns
    ``(ok, message)`` — message is actionable on failure.
    """
    which_dsh = shutil.which("dsh")
    if which_dsh is None:
        return False, (
            "dsh not found on PATH; install it with: "
            "npm install -g @deepseek-ai/dsh"
        )
    argv = [which_dsh, "--profile", profile, "--dump-config"]
    if os.name == "nt" and which_dsh.lower().endswith((".cmd", ".bat", ".ps1")):
        # Windows: npm installs a .CMD shim, which CreateProcess cannot spawn
        # directly — route through the command interpreter.
        argv = [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d", "/c", which_dsh,
            "--profile", profile, "--dump-config",
        ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_secs,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"dsh --profile {profile} --dump-config timed out after "
            f"{timeout_secs}s; retry or check for a wedged DSH_HOME."
        )
    except OSError as e:
        return False, f"could not execute dsh: {e}"
    if proc.returncode == 0:
        return True, f"dsh --profile {profile} boots OK"
    stderr = proc.stderr or ""
    if CREDENTIALS_FORMAT_SIGNATURE in stderr and ".credentials.yaml" in stderr:
        file_hint = credentials_format_hint()
        return False, file_hint or CREDENTIALS_FORMAT_HINT
    tail = "\n".join(stderr.strip().splitlines()[-10:]) or "(empty stderr)"
    return False, f"dsh --profile {profile} failed to boot:\n{tail}"


def register(reg: Any) -> None:
    """Register this harness into a plugin registry (idempotent)."""
    from ultimate_coders.agent.registry import AgentPluginSpec

    reg.register(AgentPluginSpec(
        name=AGENT_NAME,
        aliases=AGENT_ALIASES,
        factory=DeepSeekHarnessAdapter,
        api_key_env="DEEPSEEK_API_KEY",
        cli_probe="dsh",
        description=(
            "Official DeepSeek Harness CLI (dsh --profile headless); "
            "needs DEEPSEEK_API_KEY, installed via npm i -g @deepseek-ai/dsh"
        ),
    ))


class DeepSeekHarnessAdapter(AgentAdapter):
    """Adapter for the ``dsh --profile headless`` one-shot contract."""

    def name(self) -> str:
        return AGENT_NAME

    def build_request(
        self,
        prompt: str,
        working_dir: str,
        config: SandboxConfig,
        subtask_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        env_vars = config._build_env_vars()
        if config.api_key:
            env_vars.setdefault("DEEPSEEK_API_KEY", config.api_key)
        # dsh reads DEEPSEEK_BASE_URL for gateways/proxies; forward an
        # explicitly-set value only so the public default applies otherwise.
        base_url = os.environ.get("DEEPSEEK_BASE_URL")
        if base_url:
            env_vars.setdefault("DEEPSEEK_BASE_URL", base_url)

        # subtask_config hooks: profile override (e.g. a custom one-shot
        # composition). The shipped `headless` profile is the default.
        profile = str((subtask_config or {}).get("dsh_profile") or DEFAULT_PROFILE)

        return {
            "command": "dsh",
            "args": ["--profile", profile, prompt],
            "timeout_secs": config.max_cpu_seconds,
            "working_dir": working_dir,
            "env_vars": env_vars,
        }

    def parse_output(self, result: ExecResult) -> AgentOutput:
        stderr_tail = ""
        if result.stderr:
            stderr_tail = "\n".join(result.stderr.strip().splitlines()[-10:])

        if result.timed_out:
            return AgentOutput(
                summary="DeepSeek Harness execution timed out",
                success=False,
                stderr_tail=stderr_tail,
            )

        # Contract: final assistant text on stdout (may be multi-line).
        summary = result.stdout.strip()
        if not summary:
            summary = (
                f"DeepSeek Harness exited with code {result.exit_code} "
                "and no output"
            )
        elif len(summary) > 2000:
            summary = summary[:2000] + "…"

        success = result.exit_code == 0
        if (
            not success
            and result.stderr
            and CREDENTIALS_FORMAT_SIGNATURE in result.stderr
            and ".credentials.yaml" in result.stderr
        ):
            # Boot failure, not a task failure: surface the repair directly
            # instead of a raw Node stack trace. Run preflight_check() for
            # the full diagnosis.
            summary = f"{summary}\nHint: {CREDENTIALS_FORMAT_HINT}"

        return AgentOutput(
            summary=summary,
            # Contract: exit 0 exactly when the turn completed.
            success=success,
            stderr_tail=stderr_tail,
        )
