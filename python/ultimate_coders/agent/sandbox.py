"""Sandbox management for agent execution.

Provides Python wrappers for creating and managing sandbox environments
that execute coding agents (Claude Code, Codex) in isolated settings.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ultimate_coders.agent.types import ChangeType, FileChange

logger = logging.getLogger(__name__)


class NetworkMode:
    """Network access modes for sandbox execution."""
    NONE = "none"
    RESTRICTED = "restricted"
    FULL = "full"


@dataclass
class SandboxConfig:
    """Configuration for sandbox agent execution.

    Args:
        agent: Which coding agent to use ("claude-code" or "codex").
        backend: Sandbox isolation backend ("subprocess" or "docker").
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
    """
    agent: str = "claude-code"
    backend: str = "subprocess"
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

    def to_engine_config(self) -> dict[str, Any]:
        """Convert to a dict suitable for passing to the Rust engine."""
        return {
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

    def _build_env_vars(self) -> dict[str, str]:
        """Build environment variables including API keys."""
        env = dict(self.env_vars)
        if self.api_key:
            if self.agent == "claude-code":
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
            agent="claude-code",
            project_path="/path/to/project",
            api_key="sk-...",
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
        if agent == "claude-code":
            return ClaudeCodeAdapter()
        elif agent == "codex":
            return CodexAdapter()
        else:
            raise ValueError(f"Unknown agent: {agent}. Available: claude-code, codex")

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

    async def execute(self, prompt: str, working_dir: str | None = None) -> AgentOutput:
        """Execute an agent prompt in a sandbox.

        Args:
            prompt: The prompt/instruction for the agent.
            working_dir: Working directory (defaults to config.project_path).

        Returns:
            AgentOutput with summary, file changes, and success status.
        """
        handle = await self.acquire()
        wd = working_dir or self.config.working_dir or self.config.project_path

        try:
            # Create baseline for file tracking
            if self.engine is not None and hasattr(self.engine, "create_baseline"):
                await self.engine.create_baseline(wd)

            # Build and execute the agent command
            exec_request = self._adapter.build_request(
                prompt, wd, self.config,
            )

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
                # Pure Python fallback: run subprocess directly
                result = await self._execute_subprocess(exec_request)

            # Parse output
            output = self._adapter.parse_output(result)

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

    async def _execute_subprocess(self, request: dict[str, Any]) -> ExecResult:
        """Execute a command as a subprocess (pure Python fallback).

        Args:
            request: Execution request dict with command, args, etc.

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
                    result.exit_code, elapsed, len(result.stdout), len(result.stderr),
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


class AgentAdapter(ABC):
    """Base class for agent adapters."""

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def build_request(
        self, prompt: str, working_dir: str, config: SandboxConfig
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

    def build_request(self, prompt: str, working_dir: str, config: SandboxConfig) -> dict[str, Any]:
        timeout = min(config.max_cpu_seconds, 300)
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
            "env_vars": config._build_env_vars(),
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


class ClaudeCodeAdapter(AgentAdapter):
    """Adapter for Claude Code CLI."""

    def name(self) -> str:
        return "claude-code"

    def build_request(self, prompt: str, working_dir: str, config: SandboxConfig) -> dict[str, Any]:
        return {
            "command": "claude",
            "args": [
                "-p", prompt,
                "--output-format", "json",
                "--max-turns", "20",
                "--dangerously-skip-permissions",
            ],
            "timeout_secs": config.max_cpu_seconds,
            "working_dir": working_dir,
            "env_vars": config._build_env_vars(),
        }

    def parse_output(self, result: ExecResult) -> AgentOutput:
        if result.timed_out:
            return AgentOutput(
                summary="Claude Code execution timed out",
                success=False,
            )

        if result.exit_code != 0:
            return AgentOutput(
                summary=f"Claude Code exited with code {result.exit_code}: {result.stderr[:200]}",
                success=False,
            )

        # Try to parse JSON output
        import json
        output = result.stdout.strip()

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
            )

        except json.JSONDecodeError:
            return AgentOutput(
                summary=output[:1000] if output else "Claude Code completed",
                success=True,
            )


class CodexAdapter(AgentAdapter):
    """Adapter for OpenAI Codex CLI."""

    def name(self) -> str:
        return "codex"

    def build_request(self, prompt: str, working_dir: str, config: SandboxConfig) -> dict[str, Any]:
        return {
            "command": "codex",
            "args": [prompt, "--full-auto"],
            "timeout_secs": config.max_cpu_seconds,
            "working_dir": working_dir,
            "env_vars": config._build_env_vars(),
        }

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
    return ["claude-code", "claude-code-decompose", "codex"]


def create_adapter(name: str) -> AgentAdapter:
    """Create an agent adapter by name.

    Args:
        name: The agent adapter name.

    Returns:
        An AgentAdapter instance.

    Raises:
        ValueError: If the agent name is not recognized.
    """
    if name == "claude-code":
        return ClaudeCodeAdapter()
    elif name == "claude-code-decompose":
        return DecomposeAdapter()
    elif name == "codex":
        return CodexAdapter()
    raise ValueError(f"Unknown agent: {name}. Available: {available_agents()}")
