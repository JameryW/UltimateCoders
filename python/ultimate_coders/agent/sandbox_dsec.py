"""dsec — the DeepSeek sandbox environment.

``dsec`` is a named sandbox environment for the Orchestrator's Sandbox layer:
the same plugin-registry-driven execution path as every other agent, wrapped
in a ready-to-run profile whose coding agent is the official DeepSeek Harness
(``dsh --profile headless``).

What "environment" means here, concretely:

* **Selection** — ``dsec`` is a first-class name in the plugin registry
  (:func:`register`), so ``UC_CODING_AGENT=dsec`` works everywhere agent
  names are resolved: ``SandboxConfig`` defaults, adapter creation, API-key
  mapping, worker capability advertising and the deny-by-default env
  allowlist (``dsec`` is covered by the same ``DEEPSEEK_*`` allowlist entry
  as ``deepseek-harness`` — the tuple is single-sourced in
  ``ultimate_coders.agent.sandbox``).
* **Configuration** — :func:`dsec_sandbox_config` builds a
  :class:`~ultimate_coders.agent.sandbox.SandboxConfig` preconfigured for
  the DeepSeek Harness, for callers that embed the sandbox programmatically.
* **Diagnosis** — :func:`preflight_dsec` / :func:`dsec_doctor_report` validate
  the environment (dsh binary, headless profile boot, credentials) with no
  API cost, and ``python -m ultimate_coders.agent.sandbox_dsec`` prints the
  report (``--check`` turns it into a gate).

Deployment: ``./run-dsec.sh up`` (docker compose with
``docker/docker-compose.dsec.yml``) — see ``docs/dsec-sandbox.md``.

Credentials (env-based, no Web UI setup needed in containers):
    DEEPSEEK_API_KEY   — required for real runs
    DEEPSEEK_BASE_URL  — optional; defaults to the public DeepSeek API
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any

from ultimate_coders.agent import harness_deepseek
from ultimate_coders.agent.sandbox import NetworkMode, SandboxConfig

logger = logging.getLogger(__name__)

#: Environment name accepted by ``UC_CODING_AGENT`` (and ``SandboxConfig.agent``).
DSEC_ENV_NAME = "dsec"

#: The in-tree agent plugin implementing this environment.
DSEC_AGENT = harness_deepseek.AGENT_NAME

#: Env vars the DeepSeek Harness reads; the ones the sandbox env allowlist
#: forwards to the agent subprocess (single-sourced with the allowlist entry
#: in ``ultimate_coders.agent.sandbox``).
DSEC_CREDENTIAL_ENV = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")

#: Node.js floor for the dsh CLI: 0.2.0-rc.x dispatches through
#: ``if (import.meta.main)``, a flag only Node >= 22.19.0 defines. On an
#: older runtime the binary exits 0 WITHOUT running anything — including
#: every diagnostic — so an exit code alone cannot tell "boots" from
#: "silent no-op". :func:`_dsh_version` therefore requires printed output.
DSH_NODE_FLOOR = "22.19.0"

#: Default resource limits for the dsec environment (per agent subprocess).
DSEC_DEFAULTS: dict[str, Any] = {
    "max_cpu_seconds": 3600,
    "max_memory_mb": 8192,
    "network": NetworkMode.FULL,
}


def register(reg: Any) -> None:
    """Register the ``dsec`` sandbox environment into a plugin registry.

    Idempotent. ``dsec`` is registered as its own spec (same adapter factory,
    key env and CLI probe as ``deepseek-harness``) rather than as an alias of
    it, so the registry's duplicate-name rejection keeps guarding the
    canonical agent while the environment name stays independently
    discoverable in ``available_agents()`` / worker capabilities.
    """
    from ultimate_coders.agent.registry import AgentPluginSpec

    spec = AgentPluginSpec(
        name=DSEC_ENV_NAME,
        factory=harness_deepseek.DeepSeekHarnessAdapter,
        api_key_env="DEEPSEEK_API_KEY",
        cli_probe="dsh",
        description=(
            "dsec sandbox environment — official DeepSeek Harness CLI "
            "(dsh --profile headless); same agent as deepseek-harness"
        ),
    )
    reg.register(spec)


def dsec_sandbox_config(
    project_path: str = "",
    *,
    api_key: str | None = None,
    working_dir: str = "",
    **overrides: Any,
) -> SandboxConfig:
    """Build a :class:`SandboxConfig` for the dsec environment.

    Args:
        project_path: Project directory the agent operates on.
        api_key: DeepSeek API key. When given it is exported to the child as
            ``DEEPSEEK_API_KEY`` (the registry's key mapping for this agent);
            otherwise the child inherits the host value through the sandbox
            env allowlist.
        working_dir: Working directory inside the sandbox (defaults to
            ``project_path``).
        **overrides: Any other ``SandboxConfig`` field (e.g. ``network``,
            ``max_cpu_seconds``, ``env_vars``, ``mcp_configs``).

    Returns:
        A ``SandboxConfig`` whose ``agent`` is ``dsec``.
    """
    kwargs: dict[str, Any] = {
        "agent": DSEC_ENV_NAME,
        "project_path": project_path,
        "working_dir": working_dir,
        **DSEC_DEFAULTS,
    }
    if api_key is not None:
        kwargs["api_key"] = api_key
    kwargs.update(overrides)
    return SandboxConfig(**kwargs)


def dsec_child_env_allowlist(config: SandboxConfig | None = None) -> tuple[str, ...]:
    """Env names a dsec agent subprocess may inherit from the host.

    Convenience wrapper around ``SandboxConfig.child_env_allowlist`` pinned to
    the dsec agent; the DeepSeek credential variables come from the same
    single-sourced allowlist entry as ``deepseek-harness``.
    """
    cfg = config or dsec_sandbox_config()
    return cfg.child_env_allowlist(DSEC_ENV_NAME)


def _dsh_version(timeout_secs: int = 15) -> str | None:
    """Run ``dsh --version``; return its stdout, or ``None`` when it prints nothing.

    ``None`` means either "dsh not on PATH" (callers usually already know) or
    the silent no-op: on a Node runtime older than :data:`DSH_NODE_FLOOR` the
    CLI exits 0 without executing, so ``--version`` produces no output. Any
    dsh that prints a version runs on a runtime where ``import.meta.main``
    exists — and therefore where the whole CLI (including the headless
    profile) actually runs.
    """
    which_dsh = shutil.which("dsh")
    if which_dsh is None:
        return None
    try:
        proc = subprocess.run(
            [which_dsh, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_secs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (proc.stdout or "").strip() or None


def preflight_dsec(timeout_secs: int = 20) -> tuple[bool, str]:
    """Validate that the dsec environment can run tasks (no API cost).

    Checks, in order:

    1. ``DEEPSEEK_API_KEY`` is present in the environment;
    2. the ``dsh`` binary is on PATH, actually executes (``dsh --version``
       prints a version — see :data:`DSH_NODE_FLOOR` for why output and not
       just the exit code matters), and ``dsh --profile headless`` boots
       (delegates to :func:`harness_deepseek.preflight_check`);
    3. the ``$DSH_HOME/.credentials.yaml`` file is not in the structured
       shape old dsh builds reject (:func:`harness_deepseek.credentials_format_hint`).

    Returns:
        ``(ok, message)`` — the message lists every problem found and is
        actionable on failure.
    """
    problems: list[str] = []

    if not os.environ.get("DEEPSEEK_API_KEY"):
        problems.append(
            "DEEPSEEK_API_KEY is not set — dsh authenticates from the "
            "environment; export it (or set it in docker/.env for compose runs)."
        )

    if shutil.which("dsh") is not None and _dsh_version() is None:
        problems.append(
            "dsh exits 0 but prints nothing for `dsh --version` — its Node "
            f"runtime is older than the dsh engine floor (>= {DSH_NODE_FLOOR}); "
            "on such runtimes `import.meta.main` is undefined and the CLI "
            "silently does nothing (boot probes included). Upgrade Node."
        )

    boots, boot_message = harness_deepseek.preflight_check(timeout_secs=timeout_secs)
    if not boots:
        problems.append(boot_message)

    hint = harness_deepseek.credentials_format_hint()
    if hint:
        problems.append(hint)

    if problems:
        return False, "dsec environment not ready:\n- " + "\n- ".join(problems)
    return True, f"dsec environment ready ({boot_message}; DEEPSEEK_API_KEY present)"


def _mask_key(value: str) -> str:
    """Mask an API key for reports: keep the prefix shape and last 4 chars."""
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}…{value[-4:]} (len={len(value)})"


def dsec_doctor_report(timeout_secs: int = 20) -> str:
    """Human-readable dsec environment report (also printed by ``__main__``)."""
    config = dsec_sandbox_config()
    allow = dsec_child_env_allowlist(config)

    which_dsh = shutil.which("dsh")
    dsh_version = _dsh_version()
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "")
    dsh_home = harness_deepseek._dsh_home()

    ok, message = preflight_dsec(timeout_secs=timeout_secs)

    lines = [
        "dsec sandbox environment report",
        f"  environment name       : {DSEC_ENV_NAME}",
        f"  canonical agent        : {DSEC_AGENT} (dsh --profile headless)",
        f"  dsh binary             : {which_dsh or 'NOT FOUND on PATH'}",
        f"  dsh version            : {dsh_version or 'SILENT NO-OP (see preflight)'}",
        f"  DEEPSEEK_API_KEY       : {_mask_key(key) if key else 'NOT SET'}",
        f"  DEEPSEEK_BASE_URL      : {base_url or '(default: public DeepSeek API)'}",
        f"  DSH_HOME               : {dsh_home}",
        "  host env allowlist     : "
        + ", ".join(name for name in DSEC_CREDENTIAL_ENV if name in allow)
        + " pass to the agent subprocess; all other host vars are filtered",
        f"  preflight              : {'OK' if ok else 'FAILED'}",
    ]
    if not ok:
        lines.append("")
        lines.append(message)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """``python -m ultimate_coders.agent.sandbox_dsec`` entry point.

    Prints the dsec environment report. With ``--check`` the exit code gates
    on :func:`preflight_dsec` (0 = ready, 1 = not ready) — suitable for CI or
    a container healthcheck.
    """
    parser = argparse.ArgumentParser(
        prog="python -m ultimate_coders.agent.sandbox_dsec",
        description="dsec — DeepSeek sandbox environment report / preflight gate",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the environment is not ready (default: always exit 0)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="dsh boot probe timeout in seconds (default: 20)",
    )
    args = parser.parse_args(argv)

    ok, message = preflight_dsec(timeout_secs=args.timeout)

    if args.json:
        payload = {
            "environment": DSEC_ENV_NAME,
            "agent": DSEC_AGENT,
            "dsh_on_path": shutil.which("dsh"),
            "dsh_version": _dsh_version(),
            "deepseek_api_key_set": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "deepseek_base_url": os.environ.get("DEEPSEEK_BASE_URL") or None,
            "dsh_home": str(harness_deepseek._dsh_home()),
            "allowlisted_credentials": [
                name for name in DSEC_CREDENTIAL_ENV if name in dsec_child_env_allowlist()
            ],
            "ready": ok,
            "message": message,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(dsec_doctor_report(timeout_secs=args.timeout))

    if args.check and not ok:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(main())
