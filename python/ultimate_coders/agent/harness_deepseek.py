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
"""

from __future__ import annotations

import os
from typing import Any

from ultimate_coders.agent.sandbox import AgentAdapter, AgentOutput, ExecResult, SandboxConfig

AGENT_NAME = "deepseek-harness"
AGENT_ALIASES = ("deepseek",)
DEFAULT_PROFILE = "headless"


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

        return AgentOutput(
            summary=summary,
            # Contract: exit 0 exactly when the turn completed.
            success=result.exit_code == 0,
            stderr_tail=stderr_tail,
        )
