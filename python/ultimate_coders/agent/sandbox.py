"""Sandbox management for agent execution.

Provides Python wrappers for creating and managing sandbox environments
that execute coding agents (Grok Build, Claude Code, Codex) in isolated settings.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ultimate_coders.agent.types import ChangeType, FileChange

logger = logging.getLogger(__name__)

DEFAULT_CODING_AGENT = "grok-build"
GROK_AGENT_ALIASES = ("grok-build", "grok")


class NetworkMode:
    """Network access modes for sandbox execution."""
    NONE = "none"
    RESTRICTED = "restricted"
    FULL = "full"


@dataclass
class SandboxConfig:
    """Configuration for sandbox agent execution.

    Args:
        agent: Which coding agent to use ("grok-build", "claude-code", or "codex").
        project_path: Path to the project directory.
        api_key: API key for the agent (optional, can use env var).
        max_cpu_seconds: Maximum CPU time in seconds.
        max_memory_mb: Maximum memory in MB.
        max_output_bytes: Maximum output size in bytes.
        max_file_size_mb: Maximum file size in MB.
        network: Network access mode.
        warm_pool_size: Number of pre-warmed sandbox instances.
        max_pool_size: Maximum total sandbox instances.
        working_dir: Working directory inside the sandbox.
        env_vars: Additional environment variables.
        tools: Tool list for the selected agent's tools flag.
        allowed_tools: Allowed tool patterns for the selected agent.
        disallowed_tools: Disallowed tool patterns for the selected agent.
        mcp_configs: MCP server config file paths or inline server mappings.
        append_system_prompt: Extra rules/system prompt for the selected agent.
        agent_name: Custom agent name for --agent.
        agents_json: JSON string defining custom agents for --agents.
    """
    agent: str = field(
        default_factory=lambda: os.environ.get("UC_CODING_AGENT", DEFAULT_CODING_AGENT)
    )
    project_path: str = ""
    api_key: str | None = None
    max_cpu_seconds: int = 3600
    max_memory_mb: int = 8192
    max_output_bytes: int = 50 * 1024 * 1024  # 50 MB
    max_file_size_mb: int = 500
    network: str = NetworkMode.FULL
    warm_pool_size: int = 2
    max_pool_size: int = 10
    working_dir: str = ""
    env_vars: dict[str, str] = field(default_factory=dict)
    # Agent customization (translated to the selected CLI's flags/config)
    tools: list[str] | None = None              # --tools (e.g. ["default", "mcp__codegraph__*"])
    allowed_tools: list[str] | None = None      # --allowedTools
    disallowed_tools: list[str] | None = None   # --disallowedTools
    mcp_configs: list[str | dict[str, Any]] | None = None
    append_system_prompt: str | None = None
    agent_name: str | None = None                # --agent (custom agent name)
    agents_json: str | None = None               # --agents JSON string

    def to_engine_config(self) -> dict[str, Any]:
        """Convert to a dict suitable for passing to the Rust engine."""
        result: dict[str, Any] = {
            "project_path": self.project_path,
            "working_dir": self.working_dir or self.project_path,
            "env_vars": self._build_env_vars(),
            "resource_limits": {
                "max_cpu_seconds": self.max_cpu_seconds,
                "max_memory_mb": self.max_memory_mb,
                "max_output_bytes": self.max_output_bytes,
                "max_file_size_mb": self.max_file_size_mb,
            },
            "network": self.network,
        }
        # Agent customization fields (flow through to Rust SandboxConfig)
        if self.tools is not None:
            result["tools"] = self.tools
        if self.allowed_tools is not None:
            result["allowed_tools"] = self.allowed_tools
        if self.disallowed_tools is not None:
            result["disallowed_tools"] = self.disallowed_tools
        if self.mcp_configs is not None:
            result["mcp_configs"] = self.mcp_configs
        if self.append_system_prompt is not None:
            result["append_system_prompt"] = self.append_system_prompt
        if self.agent_name is not None:
            result["agent_name"] = self.agent_name
        if self.agents_json is not None:
            result["agents_json"] = self.agents_json
        return result

    def _build_env_vars(self) -> dict[str, str]:
        """Build environment variables including API keys."""
        env = dict(self.env_vars)
        if self.api_key:
            if self.agent in GROK_AGENT_ALIASES:
                env["XAI_API_KEY"] = self.api_key
            elif self.agent == "claude-code":
                env["ANTHROPIC_API_KEY"] = self.api_key
            elif self.agent == "codex":
                env["OPENAI_API_KEY"] = self.api_key
        return env


@dataclass
class SandboxHandle:
    """Handle to a sandbox instance."""
    id: str = ""
    status: str = "ready"
    created_at: int = 0


@dataclass
class ExecResult:
    """Result of executing a command in a sandbox."""
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False

    def is_success(self) -> bool:
        """Whether the execution succeeded."""
        return self.exit_code == 0 and not self.timed_out


@dataclass
class AgentOutput:
    """Structured output from an agent adapter."""
    summary: str = ""
    file_changes: list[FileChange] = field(default_factory=list)
    token_usage: TokenUsage | None = None
    success: bool = True
    # Failure context
    stderr_tail: str = ""  # last ~10 lines of stderr (for diagnostics)
    # List of tool call names extracted from the agent output
    tool_calls: list[str] = field(default_factory=list)


@dataclass
class TokenUsage:
    """Token usage from an LLM API call."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float | None = None


class SandboxManager:
    """Manages sandbox creation, execution, and cleanup.

    Provides a high-level interface for:
    - Acquiring sandbox instances from a pool
    - Executing agent commands in sandboxes
    - Tracking file changes via git diff
    - Releasing sandboxes back to the pool

    Usage:
        config = SandboxConfig(
            agent="grok-build",
            project_path="/path/to/project",
            api_key="xai-...",
        )
        manager = SandboxManager(config)
        output = await manager.execute("Fix the bug in main.rs")
    """

    def __init__(
        self,
        config: SandboxConfig,
        engine: Any = None,
    ):
        """Initialize the SandboxManager.

        Args:
            config: Sandbox configuration.
            engine: Optional engine instance for Rust-backed execution.
        """
        self.config = config
        self.engine = engine
        self._pool: list[SandboxHandle] = []
        self._active: dict[str, SandboxHandle] = {}
        self._adapter = self._create_adapter(config.agent)

    def _create_adapter(self, agent: str) -> AgentAdapter:
        """Create an agent adapter for the specified agent type."""
        if agent in GROK_AGENT_ALIASES:
            return GrokBuildAdapter()
        elif agent == "claude-code":
            return ClaudeCodeAdapter()
        elif agent == "codex":
            return CodexAdapter()
        else:
            raise ValueError(f"Unknown agent: {agent}. Available: {available_agents()}")

    async def acquire(self) -> SandboxHandle:
        """Acquire a sandbox instance from the pool or create a new one.

        Returns:
            SandboxHandle for the acquired instance.
        """
        # Try to take from pool
        if self._pool:
            handle = self._pool.pop()
            handle.status = "busy"
            self._active[handle.id] = handle
            return handle

        # Create a new sandbox
        if self.engine is not None and hasattr(self.engine, "create_sandbox"):
            config_dict = self.config.to_engine_config()
            handle_dict = self.engine.create_sandbox(**config_dict)
            handle = SandboxHandle(
                id=handle_dict.get("id", ""),
                status=handle_dict.get("status", "busy"),
                created_at=handle_dict.get("created_at", 0),
            )
        else:
            # Pure Python fallback: create a pseudo-handle
            import uuid
            handle = SandboxHandle(
                id=str(uuid.uuid4()),
                status="busy",
                created_at=0,
            )

        self._active[handle.id] = handle
        return handle

    async def execute(
        self,
        prompt: str,
        working_dir: str | None = None,
        on_stdout_line: Any | None = None,
        subtask_config: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> AgentOutput:
        """Execute an agent prompt in a sandbox.

        Args:
            prompt: The prompt/instruction for the agent.
            working_dir: Working directory (defaults to config.project_path).
            on_stdout_line: Optional async callback ``async (line: str) -> None``
                called for each stdout line during execution. Used for real-time
                streaming of tool_call/file_modified events to TUI/Dashboard.
            subtask_config: Per-subtask agent config overrides (tools, mcp, etc.)
            agent: Override the agent adapter for this call (e.g. "codex" while
                the manager's default is "grok-build"). Used by multi-step
                workflows where each step may run a different agent. None = use
                the manager's configured adapter (default behavior).

        Returns:
            AgentOutput with summary, file changes, and success status.
        """
        handle = await self.acquire()
        wd = working_dir or self.config.working_dir or self.config.project_path
        temp_files: list[str] = []

        # ponytail: per-call adapter override for multi-step workflows; falls
        # back to the manager's default adapter when not specified.
        adapter = self._create_adapter(agent) if agent else self._adapter

        try:
            # Create baseline for file tracking
            if self.engine is not None and hasattr(self.engine, "create_baseline"):
                await self.engine.create_baseline(wd)

            # Build and execute the agent command
            exec_request = adapter.build_request(
                prompt, wd, self.config,
                subtask_config=subtask_config,
            )
            temp_files = exec_request.pop("_temp_files", [])

            if self.engine is not None and hasattr(self.engine, "execute_in_sandbox"):
                result_dict = await self.engine.execute_in_sandbox(
                    handle_id=handle.id,
                    **exec_request,
                )
                result = ExecResult(
                    exit_code=result_dict.get("exit_code", -1),
                    stdout=result_dict.get("stdout", ""),
                    stderr=result_dict.get("stderr", ""),
                    duration_ms=result_dict.get("duration_ms", 0),
                    timed_out=result_dict.get("timed_out", False),
                )
            else:
                # Pure Python fallback: run subprocess with streaming
                result = await self._execute_subprocess(
                    exec_request, on_stdout_line=on_stdout_line,
                )

            # Parse output
            output = adapter.parse_output(result)

            # Attach stderr tail for failure diagnostics
            if result.stderr and not output.success:
                lines = result.stderr.rstrip().splitlines()
                output.stderr_tail = "\n".join(lines[-10:])

            # Get file changes via git diff (if baseline was created)
            if self.engine is not None and hasattr(self.engine, "get_changes_and_reset"):
                changes = await self.engine.get_changes_and_reset(wd)
                if changes:
                    output.file_changes = changes

            return output

        except Exception as e:
            logger.error("Sandbox execution failed: %s", e, exc_info=True)
            return AgentOutput(
                summary=f"Sandbox execution error: {e}",
                success=False,
            )

        finally:
            # Clean up temp files/directories (inline MCP configs, agent config).
            for path in temp_files:
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.unlink(path)
                except OSError:
                    pass
            await self.release(handle)

    async def release(self, handle: SandboxHandle) -> None:
        """Release a sandbox back to the pool.

        Args:
            handle: The sandbox handle to release.
        """
        self._active.pop(handle.id, None)
        handle.status = "ready"

        # Keep in pool if below warm size
        if len(self._pool) < self.config.warm_pool_size:
            self._pool.append(handle)
        # Otherwise, discard (Rust engine handles cleanup)

        if self.engine is not None and hasattr(self.engine, "stop_sandbox"):
            try:
                await self.engine.stop_sandbox(handle_id=handle.id)
            except Exception:
                logger.debug("Failed to stop sandbox %s", handle.id)

    async def warm_up(self) -> None:
        """Pre-warm sandbox instances for fast allocation."""
        while len(self._pool) < self.config.warm_pool_size:
            try:
                handle = await self.acquire()
                await self.release(handle)
            except Exception as e:
                logger.warning("Failed to warm up sandbox: %s", e)
                break

    async def execute_decompose(
        self,
        request: dict[str, Any],
    ) -> ExecResult:
        """Execute a decomposition request as a subprocess.

        Unlike ``execute()``, this does NOT acquire/release a sandbox
        handle or track file changes — decomposition is read-only and
        doesn't need baseline tracking.  Use this for task decomposition
        prompts; use ``execute()`` for coding tasks that modify files.

        Args:
            request: Execution request dict with command, args, etc.
                Typically built by ``DecomposeAdapter.build_request()``.

        Returns:
            ExecResult with the process output.
        """
        return await self._execute_subprocess(request)

    async def _execute_subprocess(
        self,
        request: dict[str, Any],
        on_stdout_line: Any | None = None,
    ) -> ExecResult:
        """Execute a command as a subprocess with optional stdout streaming.

        Args:
            request: Execution request dict with command, args, etc.
            on_stdout_line: Optional async callback ``async (line: str) -> None``
                called for each stdout line during execution.

        Returns:
            ExecResult with the process output.
        """
        import asyncio
        import os
        import time

        command = request.get("command", "")
        args = request.get("args", [])
        timeout_secs = request.get("timeout_secs", self.config.max_cpu_seconds)
        env_vars = request.get("env_vars", {})
        working_dir = request.get("working_dir", self.config.project_path)

        # Log the command being executed (truncate long prompts)
        display_args = []
        for a in args:
            if len(a) > 200:
                display_args.append(a[:200] + "...")
            else:
                display_args.append(a)
        logger.info(
            "Sandbox subprocess: %s %s (timeout=%ds, cwd=%s)",
            command, " ".join(display_args), timeout_secs, working_dir,
        )

        # Build environment
        env = dict(os.environ)
        env.update(env_vars)

        proc = None
        try:
            start = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
                env=env,
            )

            try:
                # Stream stdout line-by-line if callback provided
                if on_stdout_line is not None and proc.stdout is not None:
                    stdout_lines: list[str] = []
                    stderr_chunks: list[bytes] = []

                    async def _read_stderr() -> bytes:
                        """Read stderr in background."""
                        if proc.stderr is None:
                            return b""
                        while True:
                            chunk = await proc.stderr.read(4096)
                            if not chunk:
                                break
                            stderr_chunks.append(chunk)
                        return b"".join(stderr_chunks)

                    stderr_task = asyncio.create_task(_read_stderr())

                    try:
                        while True:
                            line_bytes = await asyncio.wait_for(
                                proc.stdout.readline(),
                                timeout=timeout_secs,
                            )
                            if not line_bytes:
                                break
                            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")
                            stdout_lines.append(line)
                            # Emit streaming event
                            try:
                                await on_stdout_line(line)
                            except Exception:
                                logger.debug("stdout_line callback error", exc_info=True)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        stderr_task.cancel()
                        elapsed = time.monotonic() - start
                        logger.error("Sandbox subprocess timed out during streaming")
                        return ExecResult(
                            exit_code=-1,
                            stdout="\n".join(stdout_lines),
                            stderr=f"Command timed out after {timeout_secs}s",
                            duration_ms=int(elapsed * 1000),
                            timed_out=True,
                        )

                    await proc.wait()
                    try:
                        await stderr_task
                    except Exception:
                        pass

                    elapsed = time.monotonic() - start
                    stderr_bytes = b"".join(stderr_chunks) if stderr_chunks else b""

                    result = ExecResult(
                        exit_code=proc.returncode if proc.returncode is not None else -1,
                        stdout="\n".join(stdout_lines),
                        stderr=stderr_bytes.decode("utf-8", errors="replace"),
                        duration_ms=int(elapsed * 1000),
                        timed_out=False,
                    )
                else:
                    # No streaming callback — use original communicate() path
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=timeout_secs,
                    )
                    elapsed = time.monotonic() - start

                    result = ExecResult(
                        exit_code=proc.returncode if proc.returncode is not None else -1,
                        stdout=stdout.decode("utf-8", errors="replace"),
                        stderr=stderr.decode("utf-8", errors="replace"),
                        duration_ms=int(elapsed * 1000),
                        timed_out=False,
                    )

                logger.info(
                    "Sandbox subprocess completed: exit=%d, time=%.1fs, stdout=%dB, stderr=%dB",
                    result.exit_code, elapsed,
                    len(result.stdout), len(result.stderr),
                )
                if result.exit_code != 0:
                    logger.warning(
                        "Sandbox subprocess stderr: %s",
                        result.stderr[:500] if result.stderr else "(empty)",
                    )
                return result

            except asyncio.TimeoutError:
                elapsed = time.monotonic() - start
                proc.kill()
                await proc.wait()
                logger.error(
                    "Sandbox subprocess timed out after %.1fs (limit=%ds)",
                    elapsed, timeout_secs,
                )
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Command timed out after {timeout_secs}s",
                    duration_ms=int(elapsed * 1000),
                    timed_out=True,
                )

        except Exception as e:
            logger.error("Sandbox subprocess failed: %s", e, exc_info=True)
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
                duration_ms=0,
                timed_out=False,
            )
        finally:
            # Cancellation safety: if the outer wait_for (worker.execute_subtask
            # timeout) cancels this coroutine, CancelledError propagates without
            # hitting any TimeoutError handler above (it's BaseException, not
            # Exception). Without this, the OS subprocess (the coding agent)
            # keeps running as an orphan — consuming CPU/memory and eventually
            # OOM-killing the worker (an OMP session-interruption cause).
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass


class AgentAdapter(ABC):
    """Base class for agent adapters."""

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def build_request(
        self,
        prompt: str,
        working_dir: str,
        config: SandboxConfig,
        subtask_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def parse_output(self, result: ExecResult) -> AgentOutput: ...


class DecomposeAdapter(AgentAdapter):
    """Adapter for using Claude Code CLI for task decomposition.

    Unlike ClaudeCodeAdapter (which executes coding subtasks with many turns),
    this adapter is optimized for decomposition: single-turn, strict JSON output.
    """

    def name(self) -> str:
        return "claude-code-decompose"

    def build_request(
        self,
        prompt: str,
        working_dir: str,
        config: SandboxConfig,
        subtask_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = min(config.max_cpu_seconds, 300)
        env_vars = config._build_env_vars()
        # Decomposition remains Claude-specific for backward compatibility;
        # the coding-agent default does not change its API-key contract.
        if config.api_key:
            env_vars.setdefault("ANTHROPIC_API_KEY", config.api_key)
        logger.info(
            "DecomposeAdapter: building request (timeout=%ds, cwd=%s, prompt_len=%d)",
            timeout, working_dir, len(prompt),
        )
        return {
            "command": "claude",
            "args": [
                "-p", prompt,
                "--output-format", "json",
                "--max-turns", "1",
                "--dangerously-skip-permissions",
            ],
            "timeout_secs": timeout,
            "working_dir": working_dir,
            "env_vars": env_vars,
        }

    def parse_output(self, result: ExecResult) -> AgentOutput:
        """Parse Claude Code decomposition output into a summary.

        The actual Subtask list is extracted by
        ``parse_decomposition_output()`` which is called separately
        by the Orchestrator.
        """
        if result.timed_out:
            logger.error(
                "DecomposeAdapter: timed out after %dms", result.duration_ms,
            )
            return AgentOutput(
                summary=f"Task decomposition timed out after {result.duration_ms}ms",
                success=False,
            )

        if result.exit_code != 0:
            logger.error(
                "DecomposeAdapter: exit=%d, stderr=%s",
                result.exit_code, result.stderr[:500],
            )
            return AgentOutput(
                summary=f"Decomposition failed (exit {result.exit_code}): {result.stderr[:200]}",
                success=False,
            )

        output = result.stdout.strip()
        logger.info(
            "DecomposeAdapter: success, stdout_len=%d, duration=%dms",
            len(output), result.duration_ms,
        )
        logger.debug("DecomposeAdapter raw output: %s", output[:2000])
        return AgentOutput(
            summary=truncate_str(output, 1000) if output else "Decomposition completed",
            success=True,
        )


def truncate_str(s: str, max_len: int) -> str:
    """Truncate a string to max_len with ellipsis."""
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def parse_decomposition_output(raw_stdout: str) -> list[dict[str, Any]]:
    """Parse Claude Code JSON output into a list of subtask dicts.

    Accepts the raw stdout from a ``DecomposeAdapter`` invocation and
    extracts the JSON array of subtask objects.  Handles common
    wrapping patterns (markdown code blocks, nested ``result`` key).

    Returns:
        List of dicts with keys: description, depends_on, file_constraints,
        expected_output.
    """
    import json

    text = raw_stdout.strip()
    logger.info("parse_decomposition_output: input length=%d", len(text))

    # The Claude Code JSON output may wrap the actual content in a
    # top-level object like {"type": "result", "result": "..."} or
    # {"result": "..."}.
    try:
        outer = json.loads(text)
        if isinstance(outer, dict):
            # Extract the text content from the response envelope
            if "result" in outer and isinstance(outer["result"], str):
                text = outer["result"]
                logger.info(
                    "parse_decomposition_output: extracted 'result' key (len=%d)",
                    len(text),
                )
    except json.JSONDecodeError:
        pass  # Not wrapped — proceed with raw text

    # Strip markdown code fences if present
    if "```" in text:
        lines = text.split("\n")
        json_lines: list[str] = []
        in_block = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_block = not in_block
                continue
            if in_block:
                json_lines.append(line)
        text = "\n".join(json_lines)
        logger.info(
            "parse_decomposition_output: stripped markdown fences, remaining len=%d",
            len(text),
        )

    try:
        items = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(
            "Failed to parse decomposition JSON: %s\nText preview: %s",
            e, text[:1000],
        )
        logger.debug("Full raw output: %s", raw_stdout[:2000])
        raise ValueError(f"Failed to parse decomposition output: {e}") from e

    if not isinstance(items, list):
        logger.error("Decomposition output is not a JSON array: %s", type(items).__name__)
        raise ValueError(f"Expected JSON array, got {type(items).__name__}")

    logger.info("parse_decomposition_output: parsed %d subtasks", len(items))
    return items


def _merge_agent_config(
    config: SandboxConfig,
    subtask_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge SandboxConfig agent fields with per-subtask overrides.

    Subtask-level values take precedence. Lists (tools, allowed_tools, etc.)
    are replaced, not merged — subtask config defines the full set.
    """
    result: dict[str, Any] = {}
    for key in (
        "tools", "allowed_tools", "disallowed_tools",
        "mcp_configs", "append_system_prompt",
        "agent_name", "agents_json",
    ):
        val = getattr(config, key, None)
        if val is not None:
            result[key] = val
    # Subtask overrides take precedence
    if subtask_config:
        result.update(subtask_config)
    return result


def _resolve_mcp_configs(
    mcp_configs: list[str | dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Resolve MCP configs: file paths pass through, inline JSON writes temp files.

    Returns (resolved_paths, temp_file_paths) — caller must clean up temp_file_paths.
    ponytail: temp files per-execution — upgrade path is a shared cache keyed by content hash.
    """
    resolved: list[str] = []
    temp_paths: list[str] = []
    for entry in mcp_configs:
        if isinstance(entry, dict):
            # Inline MCP config — write to temp file
            content = json.dumps({"mcpServers": entry}, indent=2)
            fd, path = tempfile.mkstemp(suffix=".mcp.json", prefix="uc-mcp-")
            with os.fdopen(fd, "w") as f:
                f.write(content)
            resolved.append(path)
            temp_paths.append(path)
        elif isinstance(entry, str):
            resolved.append(entry)
        else:
            logger.warning("Skipping invalid mcp_config entry: %r", entry)
    return resolved, temp_paths


def _codex_mcp_server_toml(name: str, cfg: dict[str, Any]) -> str:
    """Convert a single MCP server config dict to Codex config.toml section.

    Handles both stdio and streamable-http transports.
    ponytail: minimal toml — only command/args/url, no OAuth or per-tool overrides.
    """
    def _toml_escape(s: str) -> str:
        """Escape a string for TOML double-quoted value."""
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    lines = [f'[mcp_servers."{_toml_escape(name)}"]']
    if "url" in cfg:
        lines.append(f'url = "{_toml_escape(cfg["url"])}"')
        if "bearer_token_env_var" in cfg:
            lines.append(f'bearer_token_env_var = "{_toml_escape(cfg["bearer_token_env_var"])}"')
    else:
        # stdio transport
        if "command" in cfg:
            lines.append(f'command = "{_toml_escape(cfg["command"])}"')
        if "args" in cfg:
            args_str = ", ".join(f'"{_toml_escape(a)}"' for a in cfg["args"])
            lines.append(f"args = [{args_str}]")
        if "env" in cfg:
            env_pairs = ", ".join(f'{k} = "{_toml_escape(v)}"' for k, v in cfg["env"].items())
            lines.append(f"env = {{{env_pairs}}}")
    if "enabled_tools" in cfg:
        tools_str = ", ".join(f'"{_toml_escape(t)}"' for t in cfg["enabled_tools"])
        lines.append(f"enabled_tools = [{tools_str}]")
    if "disabled_tools" in cfg:
        tools_str = ", ".join(f'"{_toml_escape(t)}"' for t in cfg["disabled_tools"])
        lines.append(f"disabled_tools = [{tools_str}]")
    return "\n".join(lines)


def _grok_toml_value(value: Any) -> str:
    """Serialize the small set of TOML values used by Grok MCP configs."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_grok_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = [
            f"{json.dumps(str(key), ensure_ascii=False)} = {_grok_toml_value(item)}"
            for key, item in value.items()
        ]
        return "{ " + ", ".join(pairs) + " }"
    return json.dumps(str(value), ensure_ascii=False)


def _grok_mcp_server_toml(name: str, cfg: dict[str, Any]) -> str:
    """Convert an MCP server mapping to Grok Build's TOML configuration."""
    lines = [
        f"[mcp_servers.{json.dumps(str(name), ensure_ascii=False)}]",
    ]
    # These keys are shared by the Claude/Cursor MCP JSON shape and Grok's
    # native TOML shape. Keeping the translation explicit prevents arbitrary
    # user input from becoming a TOML table/key.
    for key in (
        "command", "args", "env", "url", "headers",
        "bearer_token_env_var", "enabled_tools", "disabled_tools",
        "enabled", "startup_timeout_sec", "tool_timeout_sec",
    ):
        if key in cfg:
            lines.append(f"{key} = {_grok_toml_value(cfg[key])}")
    return "\n".join(lines)


def _grok_mcp_config_toml(
    mcp_configs: list[str | dict[str, Any]],
) -> str:
    """Load MCP JSON entries and return a Grok ``config.toml`` fragment."""
    sections: list[str] = []
    for entry in mcp_configs:
        data: Any = entry
        if isinstance(entry, str):
            if not os.path.isfile(entry):
                logger.warning("Grok MCP config does not exist: %s", entry)
                continue
            try:
                with open(entry, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Failed to read Grok MCP config %s: %s", entry, exc)
                continue

        if not isinstance(data, dict):
            logger.warning("Skipping invalid Grok MCP config entry: %r", entry)
            continue

        # Accept native project mappings as well as the common JSON formats.
        servers = data.get(
            "mcpServers",
            data.get("mcp_servers", data.get("servers", data)),
        )
        if not isinstance(servers, dict):
            logger.warning("Skipping Grok MCP config without server mappings: %r", entry)
            continue
        for name, server_cfg in servers.items():
            if isinstance(server_cfg, dict):
                sections.append(_grok_mcp_server_toml(str(name), server_cfg))
            else:
                logger.warning("Skipping invalid Grok MCP server %s", name)

    return "\n\n".join(sections) + ("\n" if sections else "")


def _grok_tool_name(value: Any) -> str | None:
    """Extract a tool name from a Grok streaming event, if present."""
    if isinstance(value, dict):
        for key in ("name", "tool_name", "toolName"):
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        for key in ("tool_call", "toolCall", "tool_use", "toolUse", "function"):
            nested = value.get(key)
            name = _grok_tool_name(nested)
            if name:
                return name
    return None


def _grok_text(value: Any, *, _depth: int = 0) -> str:
    """Extract human-readable text from Grok JSON/streaming-json values."""
    if _depth > 5:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_grok_text(item, _depth=_depth + 1) for item in value]
        return " ".join(part for part in parts if part)
    if not isinstance(value, dict):
        return ""

    for key in (
        "result", "output_text", "text", "summary", "response",
        "message", "content", "output",
    ):
        if key in value:
            text = _grok_text(value[key], _depth=_depth + 1)
            if text:
                return text

    messages = value.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") in (None, "assistant"):
                text = _grok_text(message, _depth=_depth + 1)
                if text:
                    return text
    return ""


def _grok_usage(value: Any) -> TokenUsage | None:
    """Extract token usage from a Grok result envelope."""
    if not isinstance(value, dict):
        return None
    usage = value.get("usage", value.get("token_usage"))
    if not isinstance(usage, dict):
        return None

    def _int(*keys: str) -> int:
        for key in keys:
            try:
                return int(usage.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
        return 0

    cost = usage.get("total_cost_usd", usage.get("cost_usd"))
    try:
        cost = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost = None
    return TokenUsage(
        input_tokens=_int("input_tokens", "prompt_tokens"),
        output_tokens=_int("output_tokens", "completion_tokens"),
        total_cost_usd=cost,
    )


def _parse_agent_file_changes(output: str) -> list[FileChange]:
    """Parse the conventional Created/Modified/Deleted lines from output."""
    changes: list[FileChange] = []
    change_types = {
        "Created:": ChangeType.CREATED,
        "Modified:": ChangeType.MODIFIED,
        "Deleted:": ChangeType.DELETED,
    }
    for line in output.splitlines():
        trimmed = line.strip()
        for keyword, change_type in change_types.items():
            if trimmed.startswith(keyword):
                path = trimmed[len(keyword):].strip()
                if path:
                    changes.append(FileChange(
                        file_path=path,
                        change_type=change_type,
                        diff="",
                    ))
                break
    return changes


class GrokBuildAdapter(AgentAdapter):
    """Adapter for the xAI Grok Build terminal coding agent."""

    def name(self) -> str:
        return "grok-build"

    def build_request(
        self,
        prompt: str,
        working_dir: str,
        config: SandboxConfig,
        subtask_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg = _merge_agent_config(config, subtask_config)
        args = [
            "--no-auto-update",
            "--no-alt-screen",
            "--always-approve",
            "--cwd", working_dir,
            "--output-format", "streaming-json",
            "-p", prompt,
        ]

        if cfg.get("tools"):
            tools = cfg["tools"] if isinstance(cfg["tools"], list) else [cfg["tools"]]
            # Claude-compatible MCP names are mcp__server__tool; Grok names
            # the same tools server__tool.
            normalized = [
                str(tool)[len("mcp__"):]
                if str(tool).startswith("mcp__") else str(tool)
                for tool in tools
            ]
            args += ["--tools", ",".join(normalized)]
        if cfg.get("allowed_tools"):
            allowed = (
                cfg["allowed_tools"]
                if isinstance(cfg["allowed_tools"], list)
                else [cfg["allowed_tools"]]
            )
            for rule in allowed:
                args += ["--allow", str(rule)]
        if cfg.get("disallowed_tools"):
            denied = (
                cfg["disallowed_tools"]
                if isinstance(cfg["disallowed_tools"], list)
                else [cfg["disallowed_tools"]]
            )
            args += ["--disallowed-tools", ",".join(str(rule) for rule in denied)]
        if cfg.get("append_system_prompt"):
            args += ["--rules", str(cfg["append_system_prompt"])]
        if cfg.get("agent_name"):
            args += ["--agent", str(cfg["agent_name"])]
        if cfg.get("agents_json"):
            agents_json = cfg["agents_json"]
            if not isinstance(agents_json, str):
                agents_json = json.dumps(agents_json, ensure_ascii=False)
            args += ["--agents", agents_json]

        env_vars = config._build_env_vars()
        # A workflow may select Grok for one step while the manager was
        # constructed with another adapter. Preserve the explicit API key in
        # that case without changing the manager-wide environment.
        if config.api_key:
            env_vars.setdefault("XAI_API_KEY", config.api_key)

        temp_files: list[str] = []
        mcp_configs = cfg.get("mcp_configs")
        if mcp_configs:
            mcp_toml = _grok_mcp_config_toml(mcp_configs)
            if mcp_toml:
                grok_home = tempfile.mkdtemp(prefix="uc-grok-")
                source_home = env_vars.get(
                    "GROK_HOME",
                    os.environ.get("GROK_HOME", os.path.expanduser("~/.grok")),
                )
                auth_path = os.path.join(source_home, "auth.json")
                if os.path.isfile(auth_path):
                    try:
                        shutil.copy2(auth_path, os.path.join(grok_home, "auth.json"))
                    except OSError as exc:
                        logger.warning("Could not copy Grok auth cache: %s", exc)
                with open(os.path.join(grok_home, "config.toml"), "w", encoding="utf-8") as f:
                    f.write(mcp_toml)
                env_vars["GROK_HOME"] = grok_home
                temp_files.append(grok_home)

        return {
            "command": "grok",
            "args": args,
            "timeout_secs": config.max_cpu_seconds,
            "working_dir": working_dir,
            "env_vars": env_vars,
            "_temp_files": temp_files,
        }

    @staticmethod
    def _json_events(output: str) -> list[dict[str, Any]]:
        """Decode either a single JSON envelope or newline-delimited events."""
        if not output:
            return []
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                return [parsed]
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass

        events: list[dict[str, Any]] = []
        for line in output.splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events

    def parse_output(self, result: ExecResult) -> AgentOutput:
        stderr_tail = ""
        if result.stderr:
            stderr_tail = "\n".join(result.stderr.strip().splitlines()[-10:])

        if result.timed_out:
            return AgentOutput(
                summary="Grok Build execution timed out",
                success=False,
                stderr_tail=stderr_tail,
            )
        if result.exit_code != 0:
            return AgentOutput(
                summary=f"Grok Build exited with code {result.exit_code}: {result.stderr[:200]}",
                success=False,
                stderr_tail=stderr_tail,
            )

        output = result.stdout.strip()
        events = self._json_events(output)
        if not events:
            return AgentOutput(
                summary=output[:1000] if output else "Grok Build completed",
                file_changes=_parse_agent_file_changes(output),
                success=True,
                stderr_tail=stderr_tail,
            )

        final_summary = ""
        last_summary = ""
        message_chunks: list[str] = []
        tool_calls: list[str] = []
        token_usage: TokenUsage | None = None
        for event in events:
            event_type = str(
                event.get("type", event.get("event", event.get("kind", "")))
            ).lower()
            tool_name = _grok_tool_name(event)
            if tool_name and ("tool" in event_type or "call" in event_type):
                tool_calls.append(tool_name)

            usage = _grok_usage(event)
            if usage is not None:
                token_usage = usage

            text = _grok_text(event)
            if not text:
                continue
            if "thought" in event_type or "tool" in event_type:
                continue
            if "chunk" in event_type and "message" in event_type:
                message_chunks.append(text)
            elif event_type in ("result", "final", "completion", "done") or "result" in event:
                final_summary = text
            else:
                last_summary = text

        summary = final_summary or ("".join(message_chunks) if message_chunks else last_summary)
        return AgentOutput(
            summary=truncate_str(summary, 1000) if summary else "Grok Build completed",
            file_changes=_parse_agent_file_changes(output),
            token_usage=token_usage,
            success=True,
            stderr_tail=stderr_tail,
            tool_calls=tool_calls[-5:],
        )


class ClaudeCodeAdapter(AgentAdapter):
    """Adapter for Claude Code CLI."""

    def name(self) -> str:
        return "claude-code"

    def build_request(
        self,
        prompt: str,
        working_dir: str,
        config: SandboxConfig,
        subtask_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        args = [
            "-p", prompt,
            "--output-format", "stream-json",
            "--max-turns", "20",
            "--dangerously-skip-permissions",
        ]

        # Merge config-level and subtask-level agent overrides
        cfg = _merge_agent_config(config, subtask_config)

        temp_files: list[str] = []

        if cfg.get("tools"):
            args += ["--tools"] + cfg["tools"]
        if cfg.get("allowed_tools"):
            args += ["--allowedTools"] + cfg["allowed_tools"]
        if cfg.get("disallowed_tools"):
            args += ["--disallowedTools"] + cfg["disallowed_tools"]
        if cfg.get("mcp_configs"):
            # Resolve inline JSON MCP configs to temp files
            resolved, temps = _resolve_mcp_configs(cfg["mcp_configs"])
            temp_files.extend(temps)
            args += ["--mcp-config"] + resolved
        if cfg.get("append_system_prompt"):
            args += ["--append-system-prompt", cfg["append_system_prompt"]]
        if cfg.get("agent_name"):
            args += ["--agent", cfg["agent_name"]]
        if cfg.get("agents_json"):
            args += ["--agents", cfg["agents_json"]]

        return {
            "command": "claude",
            "args": args,
            "timeout_secs": config.max_cpu_seconds,
            "working_dir": working_dir,
            "env_vars": config._build_env_vars(),
            "_temp_files": temp_files,
        }

    def parse_output(self, result: ExecResult) -> AgentOutput:
        # ponytail: extract last 10 lines of stderr for failure diagnostics
        stderr_tail = ""
        if result.stderr:
            stderr_lines = result.stderr.strip().splitlines()
            stderr_tail = "\n".join(stderr_lines[-10:])

        if result.timed_out:
            return AgentOutput(
                summary="Claude Code execution timed out",
                success=False,
                stderr_tail=stderr_tail,
            )

        if result.exit_code != 0:
            return AgentOutput(
                summary=f"Claude Code exited with code {result.exit_code}: {result.stderr[:200]}",
                success=False,
                stderr_tail=stderr_tail,
            )

        import json
        output = result.stdout.strip()

        # ── Try stream-json format (one JSON event per line) ──────
        # When using --output-format stream-json, stdout is a series of
        # newline-delimited JSON events.  The final event has type "result".
        # Earlier events are tool_use / tool_result / assistant messages.
        if output.startswith("{") and "\n{" in output:
            summary = ""
            tool_calls: list[str] = []
            token_usage = None

            for line in output.splitlines():
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                evt_type = obj.get("type", "")
                if evt_type == "result":
                    summary = str(obj.get("result", ""))[:500]
                    if "usage" in obj:
                        u = obj["usage"]
                        token_usage = TokenUsage(
                            input_tokens=u.get("input_tokens", 0),
                            output_tokens=u.get("output_tokens", 0),
                            total_cost_usd=u.get("total_cost_usd"),
                        )
                elif evt_type == "assistant":
                    content = obj.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_calls.append(block.get("name", "unknown"))
                        if not summary:
                            texts = [
                                b.get("text", "")
                                for b in content
                                if isinstance(b, dict)
                                and b.get("type") == "text"
                            ]
                            if texts:
                                summary = " ".join(texts)[:500]

            if not summary:
                summary = "Claude Code completed"

            return AgentOutput(
                summary=summary,
                token_usage=token_usage,
                success=True,
                stderr_tail=stderr_tail,
                tool_calls=tool_calls[-5:],
            )

        # ── Try single-JSON format (legacy --output-format json) ───
        try:
            parsed = json.loads(output)
            summary = ""
            if "result" in parsed:
                summary = str(parsed["result"])[:500]
            elif "message" in parsed:
                summary = str(parsed["message"])[:500]
            elif "messages" in parsed:
                # Take last assistant message
                for msg in reversed(parsed["messages"]):
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            texts = [b.get("text", "") for b in content if isinstance(b, dict)]
                            summary = " ".join(texts)[:500]
                        else:
                            summary = str(content)[:500]
                        break

            if not summary:
                summary = "Claude Code completed"

            # Extract tool call names from messages for failure context
            tool_calls: list[str] = []
            if "messages" in parsed:
                for msg in parsed["messages"]:
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "tool_use":
                                    tool_calls.append(block.get("name", "unknown"))

            # Extract token usage
            token_usage = None
            if "usage" in parsed:
                usage = parsed["usage"]
                token_usage = TokenUsage(
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    total_cost_usd=usage.get("total_cost_usd"),
                )

            return AgentOutput(
                summary=summary,
                token_usage=token_usage,
                success=True,
                stderr_tail=stderr_tail,
                tool_calls=tool_calls[-5:],  # ponytail: keep last 5 tool calls
            )

        except json.JSONDecodeError:
            return AgentOutput(
                summary=output[:1000] if output else "Claude Code completed",
                success=True,
            )


class CodexAdapter(AgentAdapter):
    """Adapter for OpenAI Codex CLI.

    Codex extends tools via config.toml (not CLI flags like Claude Code).
    This adapter writes a temporary config.toml with MCP servers and tool
    settings derived from subtask_config, then cleans up after execution.
    """

    def name(self) -> str:
        return "codex"

    def build_request(
        self,
        prompt: str,
        working_dir: str,
        config: SandboxConfig,
        subtask_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Merge config-level and subtask-level agent overrides
        cfg = _merge_agent_config(config, subtask_config)

        temp_files: list[str] = []

        # Build temporary config.profile if any agent customization is needed
        # Codex --profile flag: layers $CODEX_HOME/<name>.config.toml on top of base config.
        # We write a temp .config.toml in CODEX_HOME and pass the stem as --profile <name>.
        codex_home = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
        config_content = self._build_config_toml(cfg)
        profile_name: str | None = None
        env_vars = config._build_env_vars()
        if config_content:
            # Must place the file in CODEX_HOME and name it <name>.config.toml
            # so that --profile <name> resolves to $CODEX_HOME/<name>.config.toml
            target_dir = (
                codex_home
                if os.path.isdir(codex_home) and os.access(codex_home, os.W_OK)
                else None
            )
            try:
                fd, config_path = tempfile.mkstemp(
                    suffix=".config.toml", prefix="uc-codex-",
                    dir=target_dir,
                )
            except OSError:
                # A read-only CODEX_HOME is common in managed workers. Put the
                # profile in a writable temp directory and point this process
                # at it so --profile still resolves correctly.
                fd, config_path = tempfile.mkstemp(
                    suffix=".config.toml", prefix="uc-codex-",
                )
                target_dir = None
            with os.fdopen(fd, "w") as f:
                f.write(config_content)
            temp_files.append(config_path)
            if target_dir is None:
                env_vars["CODEX_HOME"] = os.path.dirname(config_path)
            # Extract profile name: filename without .config.toml suffix
            basename = os.path.basename(config_path)
            profile_name = basename[: -len(".config.toml")]

        args = [prompt, "--sandbox", "workspace-write"]
        if profile_name:
            args += ["--profile", profile_name]

        return {
            "command": "codex",
            "args": args,
            "timeout_secs": config.max_cpu_seconds,
            "working_dir": working_dir,
            "env_vars": env_vars,
            "_temp_files": temp_files,
        }

    @staticmethod
    def _build_config_toml(
        cfg: dict[str, Any],
    ) -> str:
        """Build Codex config.toml content from merged agent config.

        Returns empty string if no customization is needed.
        ponytail: minimal toml — only MCP servers and tool settings.
        """
        sections: list[str] = []

        # MCP servers from mcp_configs
        mcp_configs = cfg.get("mcp_configs", [])
        if mcp_configs:
            for entry in mcp_configs:
                if isinstance(entry, str):
                    # File path — read and merge
                    if os.path.isfile(entry):
                        try:
                            with open(entry) as f:
                                data = json.load(f)
                            for name, server_cfg in data.get("mcpServers", {}).items():
                                sections.append(
                                    _codex_mcp_server_toml(name, server_cfg)
                                )
                        except (json.JSONDecodeError, OSError):
                            logger.warning("Failed to read MCP config: %s", entry)
                elif isinstance(entry, dict):
                    # Inline config — each key is a server name
                    for name, server_cfg in entry.items():
                        sections.append(
                            _codex_mcp_server_toml(name, server_cfg)
                        )

        # Tool allow/deny lists — Codex only supports per-MCP-server tool filters,
        # not global allow/deny. Log a warning if global filters are specified.
        allowed = cfg.get("allowed_tools", [])
        disallowed = cfg.get("disallowed_tools", [])
        if allowed or disallowed:
            logger.warning(
                "Codex adapter does not support global allowed/disallowed_tools; "
                "use per-MCP-server enabled_tools/disabled_tools in mcp_configs instead. "
                "allowed=%s disallowed=%s",
                allowed, disallowed,
            )

        if not sections:
            return ""

        return "\n".join(sections) + "\n"

    def parse_output(self, result: ExecResult) -> AgentOutput:
        if result.timed_out:
            return AgentOutput(
                summary="Codex execution timed out",
                success=False,
            )

        if result.exit_code != 0:
            return AgentOutput(
                summary=f"Codex exited with code {result.exit_code}: {result.stderr[:200]}",
                success=False,
            )

        output = result.stdout.strip()
        summary_lines = []
        file_changes = []

        for line in output.split("\n"):
            trimmed = line.strip()
            # Look for file path patterns
            for keyword in ("Created:", "Modified:", "Deleted:"):
                if trimmed.startswith(keyword):
                    path = trimmed[len(keyword):].strip()
                    if path:
                        change_type_map = {
                            "Created:": ChangeType.CREATED,
                            "Modified:": ChangeType.MODIFIED,
                            "Deleted:": ChangeType.DELETED,
                        }
                        file_changes.append(FileChange(
                            file_path=path,
                            change_type=change_type_map[keyword],
                            diff="",
                        ))
                    break
            else:
                if trimmed:
                    summary_lines.append(trimmed)

        summary = "\n".join(summary_lines[:5]) if summary_lines else "Codex completed execution"

        return AgentOutput(
            summary=summary,
            file_changes=file_changes,
            success=True,
        )


def available_agents() -> list[str]:
    """List available agent adapter names."""
    return [
        "grok-build", "grok",
        "claude-code", "claude-code-decompose", "codex",
    ]


def create_adapter(name: str) -> AgentAdapter:
    """Create an agent adapter by name.

    Args:
        name: The agent adapter name.

    Returns:
        An AgentAdapter instance.

    Raises:
        ValueError: If the agent name is not recognized.
    """
    if name in GROK_AGENT_ALIASES:
        return GrokBuildAdapter()
    elif name == "claude-code":
        return ClaudeCodeAdapter()
    elif name == "claude-code-decompose":
        return DecomposeAdapter()
    elif name == "codex":
        return CodexAdapter()
    raise ValueError(f"Unknown agent: {name}. Available: {available_agents()}")
