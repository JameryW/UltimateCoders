"""Unit tests for the sandbox agent executor module."""

from __future__ import annotations

import json

import pytest
from ultimate_coders.agent.sandbox import (
    AgentOutput,
    ClaudeCodeAdapter,
    CodexAdapter,
    DecomposeAdapter,
    ExecResult,
    NetworkMode,
    SandboxConfig,
    SandboxManager,
    _merge_agent_config,
    available_agents,
    create_adapter,
    parse_decomposition_output,
    truncate_str,
)
from ultimate_coders.agent.types import ChangeType, FileChange, Subtask, Task

# ── SandboxConfig tests ─────────────────────────────────────────

class TestSandboxConfig:
    """Tests for SandboxConfig."""

    def test_defaults(self):
        config = SandboxConfig()
        assert config.agent == "claude-code"
        assert config.backend == "subprocess"
        assert config.project_path == ""
        assert config.api_key is None
        assert config.max_cpu_seconds == 3600
        assert config.max_memory_mb == 8192
        assert config.max_output_bytes == 50 * 1024 * 1024
        assert config.max_file_size_mb == 500
        assert config.network == NetworkMode.FULL
        assert config.warm_pool_size == 2
        assert config.max_pool_size == 10

    def test_custom(self):
        config = SandboxConfig(
            agent="codex",
            backend="docker",
            project_path="/tmp/project",
            api_key="sk-test",
            max_cpu_seconds=600,
            network=NetworkMode.NONE,
        )
        assert config.agent == "codex"
        assert config.backend == "docker"
        assert config.project_path == "/tmp/project"
        assert config.api_key == "sk-test"
        assert config.max_cpu_seconds == 600
        assert config.network == NetworkMode.NONE

    def test_to_engine_config(self):
        config = SandboxConfig(
            project_path="/tmp/project",
            agent="claude-code",
            api_key="sk-test",
        )
        engine_config = config.to_engine_config()
        assert engine_config["project_path"] == "/tmp/project"
        assert "ANTHROPIC_API_KEY" in engine_config["env_vars"]
        assert engine_config["env_vars"]["ANTHROPIC_API_KEY"] == "sk-test"
        assert engine_config["network"] == NetworkMode.FULL

    def test_to_engine_config_codex(self):
        config = SandboxConfig(
            agent="codex",
            api_key="sk-openai",
        )
        engine_config = config.to_engine_config()
        assert "OPENAI_API_KEY" in engine_config["env_vars"]

    def test_build_env_vars_no_key(self):
        config = SandboxConfig(agent="claude-code")
        env = config._build_env_vars()
        assert "ANTHROPIC_API_KEY" not in env

    def test_build_env_vars_with_extra(self):
        config = SandboxConfig(
            env_vars={"CUSTOM_VAR": "value"},
            api_key="sk-test",
        )
        env = config._build_env_vars()
        assert env["CUSTOM_VAR"] == "value"
        assert env["ANTHROPIC_API_KEY"] == "sk-test"


# ── ExecResult tests ────────────────────────────────────────────

class TestExecResult:
    """Tests for ExecResult."""

    def test_is_success(self):
        result = ExecResult(exit_code=0, timed_out=False)
        assert result.is_success()

    def test_is_success_nonzero(self):
        result = ExecResult(exit_code=1, timed_out=False)
        assert not result.is_success()

    def test_is_success_timed_out(self):
        result = ExecResult(exit_code=0, timed_out=True)
        assert not result.is_success()


# ── AgentOutput tests ────────────────────────────────────────────

class TestAgentOutput:
    """Tests for AgentOutput."""

    def test_defaults(self):
        output = AgentOutput()
        assert output.summary == ""
        assert output.file_changes == []
        assert output.token_usage is None
        assert output.success is True

    def test_with_changes(self):
        changes = [FileChange(file_path="main.rs", change_type=ChangeType.MODIFIED)]
        output = AgentOutput(
            summary="Fixed bug",
            file_changes=changes,
            success=True,
        )
        assert len(output.file_changes) == 1
        assert output.file_changes[0].file_path == "main.rs"


# ── ClaudeCodeAdapter tests ─────────────────────────────────────

class TestClaudeCodeAdapter:
    """Tests for ClaudeCodeAdapter."""

    def test_name(self):
        adapter = ClaudeCodeAdapter()
        assert adapter.name() == "claude-code"

    def test_build_request(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(project_path="/tmp/project")
        request = adapter.build_request("Fix the bug", "/tmp/project", config)

        assert request["command"] == "claude"
        assert "-p" in request["args"]
        assert "Fix the bug" in request["args"]
        assert "--output-format" in request["args"]
        assert "stream-json" in request["args"]
        assert "--dangerously-skip-permissions" in request["args"]
        assert request["working_dir"] == "/tmp/project"

    def test_parse_output_success(self):
        adapter = ClaudeCodeAdapter()
        result = ExecResult(
            exit_code=0,
            stdout='{"result": "Fixed the bug"}',
        )
        output = adapter.parse_output(result)
        assert output.success
        assert "Fixed the bug" in output.summary

    def test_parse_output_timeout(self):
        adapter = ClaudeCodeAdapter()
        result = ExecResult(exit_code=-1, timed_out=True)
        output = adapter.parse_output(result)
        assert not output.success
        assert "timed out" in output.summary

    def test_parse_output_failure(self):
        adapter = ClaudeCodeAdapter()
        result = ExecResult(exit_code=1, stderr="API error")
        output = adapter.parse_output(result)
        assert not output.success
        assert "exited with code 1" in output.summary

    def test_parse_output_non_json(self):
        adapter = ClaudeCodeAdapter()
        result = ExecResult(
            exit_code=0,
            stdout="I fixed the bug by editing main.rs",
        )
        output = adapter.parse_output(result)
        assert output.success
        assert "fixed the bug" in output.summary


# ── CodexAdapter tests ──────────────────────────────────────────

class TestCodexAdapter:
    """Tests for CodexAdapter."""

    def test_name(self):
        adapter = CodexAdapter()
        assert adapter.name() == "codex"

    def test_build_request(self):
        adapter = CodexAdapter()
        config = SandboxConfig(project_path="/tmp/project")
        request = adapter.build_request("Implement feature", "/tmp/project", config)

        assert request["command"] == "codex"
        assert "Implement feature" in request["args"]
        assert "--full-auto" in request["args"]

    def test_parse_output_success(self):
        adapter = CodexAdapter()
        result = ExecResult(
            exit_code=0,
            stdout="Implemented feature.\nCreated: src/feature.rs",
        )
        output = adapter.parse_output(result)
        assert output.success
        assert len(output.file_changes) == 1
        assert output.file_changes[0].file_path == "src/feature.rs"
        assert output.file_changes[0].change_type == ChangeType.CREATED

    def test_parse_output_timeout(self):
        adapter = CodexAdapter()
        result = ExecResult(exit_code=-1, timed_out=True)
        output = adapter.parse_output(result)
        assert not output.success

    def test_parse_output_failure(self):
        adapter = CodexAdapter()
        result = ExecResult(exit_code=1, stderr="Error")
        output = adapter.parse_output(result)
        assert not output.success


# ── DecomposeAdapter tests ──────────────────────────────────────

class TestDecomposeAdapter:
    """Tests for DecomposeAdapter."""

    def test_name(self):
        adapter = DecomposeAdapter()
        assert adapter.name() == "claude-code-decompose"

    def test_build_request(self):
        adapter = DecomposeAdapter()
        config = SandboxConfig(project_path="/tmp/project")
        request = adapter.build_request("Decompose this task", "/tmp/project", config)

        assert request["command"] == "claude"
        assert "-p" in request["args"]
        assert "Decompose this task" in request["args"]
        assert "--output-format" in request["args"]
        assert "json" in request["args"]
        assert "--max-turns" in request["args"]
        assert "1" in request["args"]  # Single turn for decomposition
        assert "--dangerously-skip-permissions" in request["args"]
        assert request["timeout_secs"] <= 300  # Capped at 300s
        assert request["working_dir"] == "/tmp/project"

    def test_parse_output_success(self):
        adapter = DecomposeAdapter()
        result = ExecResult(
            exit_code=0,
            stdout='[{"description": "Fix bug", "depends_on": []}]',
        )
        output = adapter.parse_output(result)
        assert output.success
        assert "Fix bug" in output.summary

    def test_parse_output_timeout(self):
        adapter = DecomposeAdapter()
        result = ExecResult(exit_code=-1, timed_out=True)
        output = adapter.parse_output(result)
        assert not output.success
        assert "timed out" in output.summary

    def test_parse_output_failure(self):
        adapter = DecomposeAdapter()
        result = ExecResult(exit_code=1, stderr="API error")
        output = adapter.parse_output(result)
        assert not output.success
        assert "exit 1" in output.summary


# ── parse_decomposition_output tests ─────────────────────────────

class TestParseDecompositionOutput:
    """Tests for parse_decomposition_output()."""

    def test_parse_simple_json(self):
        raw = json.dumps(
            [
                {
                    "description": "Fix bug",
                    "depends_on": [],
                    "file_constraints": [],
                    "expected_output": "Bug fixed",
                },
            ]
        )
        items = parse_decomposition_output(raw)
        assert len(items) == 1
        assert items[0]["description"] == "Fix bug"

    def test_parse_multiple_subtasks(self):
        raw = json.dumps(
            [
                {"description": "Research", "depends_on": []},
                {"description": "Implement", "depends_on": [0]},
            ]
        )
        items = parse_decomposition_output(raw)
        assert len(items) == 2
        assert items[1]["depends_on"] == [0]

    def test_parse_wrapped_result(self):
        """Claude Code wraps output in {"result": "..."} envelope."""
        inner = '[{"description": "Fix bug", "depends_on": []}]'
        import json as _json
        raw = _json.dumps({"type": "result", "result": inner})
        items = parse_decomposition_output(raw)
        assert len(items) == 1
        assert items[0]["description"] == "Fix bug"

    def test_parse_markdown_code_block(self):
        raw = 'Here are the subtasks:\n```json\n[{"description": "Fix bug", "depends_on": []}]\n```'
        items = parse_decomposition_output(raw)
        assert len(items) == 1

    def test_parse_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Failed to parse"):
            parse_decomposition_output("not json at all")

    def test_parse_non_array_raises(self):
        with pytest.raises(ValueError, match="Expected JSON array"):
            parse_decomposition_output('{"key": "value"}')


# ── truncate_str tests ──────────────────────────────────────────

class TestTruncateStr:
    """Tests for truncate_str()."""

    def test_short_string(self):
        assert truncate_str("hello", 10) == "hello"

    def test_exact_length(self):
        assert truncate_str("hello", 5) == "hello"

    def test_truncation(self):
        result = truncate_str("hello world", 8)
        assert result == "hello..."
        assert len(result) == 8


class TestSandboxManager:
    """Tests for SandboxManager."""

    def test_init(self):
        config = SandboxConfig(project_path="/tmp/project")
        manager = SandboxManager(config)
        assert manager.config.project_path == "/tmp/project"
        assert manager._pool == []
        assert manager._active == {}

    def test_init_invalid_agent(self):
        config = SandboxConfig(agent="invalid-agent")
        with pytest.raises(ValueError, match="Unknown agent"):
            SandboxManager(config)

    @pytest.mark.asyncio
    async def test_acquire_creates_handle(self):
        config = SandboxConfig(project_path="/tmp/project")
        manager = SandboxManager(config)
        handle = await manager.acquire()
        assert handle.id
        assert handle.status == "busy"
        assert handle.id in manager._active

    @pytest.mark.asyncio
    async def test_release_returns_to_pool(self):
        config = SandboxConfig(project_path="/tmp/project", warm_pool_size=2)
        manager = SandboxManager(config)
        handle = await manager.acquire()
        await manager.release(handle)
        assert len(manager._pool) == 1
        assert handle.id not in manager._active

    @pytest.mark.asyncio
    async def test_release_respects_warm_size(self):
        config = SandboxConfig(project_path="/tmp/project", warm_pool_size=1)
        manager = SandboxManager(config)

        # Acquire and release 3 handles
        handles = []
        for _ in range(3):
            h = await manager.acquire()
            handles.append(h)

        for h in handles:
            await manager.release(h)

        # Only warm_pool_size should remain in pool
        assert len(manager._pool) == 1

    @pytest.mark.asyncio
    async def test_pool_reuse(self):
        config = SandboxConfig(project_path="/tmp/project", warm_pool_size=2)
        manager = SandboxManager(config)

        # Acquire and release
        handle1 = await manager.acquire()
        await manager.release(handle1)

        # Acquire again -- should reuse
        handle2 = await manager.acquire()
        assert handle2.id == handle1.id


# ── create_adapter and available_agents tests ───────────────────

class TestAdapterFactory:
    """Tests for adapter factory functions."""

    def test_available_agents(self):
        agents = available_agents()
        assert "claude-code" in agents
        assert "claude-code-decompose" in agents
        assert "codex" in agents

    def test_create_adapter_claude_code(self):
        adapter = create_adapter("claude-code")
        assert isinstance(adapter, ClaudeCodeAdapter)

    def test_create_adapter_decompose(self):
        adapter = create_adapter("claude-code-decompose")
        assert isinstance(adapter, DecomposeAdapter)

    def test_create_adapter_codex(self):
        adapter = create_adapter("codex")
        assert isinstance(adapter, CodexAdapter)

    def test_create_adapter_unknown(self):
        with pytest.raises(ValueError, match="Unknown agent"):
            create_adapter("unknown")


# ── Worker sandbox mode integration test ─────────────────────────

class TestWorkerSandboxMode:
    """Tests for Worker sandbox execution (always sandbox)."""

    def test_worker_init_with_sandbox_config(self):
        from ultimate_coders.agent.worker import Worker
        config = SandboxConfig(
            agent="claude-code",
            project_path="/tmp/project",
        )
        worker = Worker(
            worker_id="w-sandbox",
            sandbox_config=config,
        )
        assert worker._sandbox_manager is not None

    def test_worker_init_default_sandbox(self):
        from ultimate_coders.agent.worker import Worker
        worker = Worker(worker_id="w-default")
        assert worker._sandbox_manager is not None


# ── Worker capability derivation tests ──────────────────────────────

class TestWorkerDeriveCapabilities:
    """Tests for Worker._derive_capabilities from SandboxConfig."""

    def test_default_capabilities(self):
        from ultimate_coders.agent.worker import Worker
        worker = Worker(worker_id="w-caps-default")
        assert "code" in worker.capabilities
        assert "search" in worker.capabilities
        assert "memory" in worker.capabilities
        assert "test" in worker.capabilities

    def test_mcp_capability_when_mcp_configs_set(self):
        from ultimate_coders.agent.worker import Worker
        config = SandboxConfig(mcp_configs=["/etc/mcp/codegraph.json"])
        worker = Worker(worker_id="w-caps-mcp", sandbox_config=config)
        assert "mcp" in worker.capabilities

    def test_no_mcp_capability_without_mcp_configs(self):
        from ultimate_coders.agent.worker import Worker
        config = SandboxConfig()
        worker = Worker(worker_id="w-caps-no-mcp", sandbox_config=config)
        assert "mcp" not in worker.capabilities

    def test_codegraph_capability_when_tool_present(self):
        from ultimate_coders.agent.worker import Worker
        config = SandboxConfig(tools=["default", "mcp__codegraph__*"])
        worker = Worker(worker_id="w-caps-cg", sandbox_config=config)
        assert "codegraph" in worker.capabilities

    def test_no_codegraph_capability_without_tool(self):
        from ultimate_coders.agent.worker import Worker
        config = SandboxConfig(tools=["default"])
        worker = Worker(worker_id="w-caps-no-cg", sandbox_config=config)
        assert "codegraph" not in worker.capabilities

    def test_agent_name_capability(self):
        from ultimate_coders.agent.worker import Worker
        config = SandboxConfig(agent_name="reviewer")
        worker = Worker(worker_id="w-caps-agent", sandbox_config=config)
        assert "agent:reviewer" in worker.capabilities

    def test_explicit_capabilities_override_derived(self):
        from ultimate_coders.agent.worker import Worker
        config = SandboxConfig(mcp_configs=["/a.json"])
        worker = Worker(
            worker_id="w-caps-override",
            sandbox_config=config,
            capabilities=["code"],
        )
        # Explicit capabilities list should be used as-is
        assert worker.capabilities == ["code"]


# ── NetworkMode tests ────────────────────────────────────────────

class TestNetworkMode:
    """Tests for NetworkMode."""

    def test_values(self):
        assert NetworkMode.NONE == "none"
        assert NetworkMode.RESTRICTED == "restricted"
        assert NetworkMode.FULL == "full"


# ── Agent tool/skill/mcp config tests ──────────────────────────────

class TestSandboxConfigAgentFields:
    """Tests for SandboxConfig tool/skill/mcp fields."""

    def test_default_agent_fields_are_none(self):
        config = SandboxConfig()
        assert config.tools is None
        assert config.allowed_tools is None
        assert config.disallowed_tools is None
        assert config.mcp_configs is None
        assert config.append_system_prompt is None
        assert config.agent_name is None
        assert config.agents_json is None

    def test_custom_agent_fields(self):
        config = SandboxConfig(
            tools=["default", "mcp__codegraph__*"],
            allowed_tools=["Bash(git *)", "Edit"],
            disallowed_tools=["Bash(rm *)"],
            mcp_configs=["/etc/mcp/codegraph.json"],
            append_system_prompt="Focus on Rust code",
            agent_name="reviewer",
            agents_json='{"reviewer": {"description": "Reviews code"}}',
        )
        assert config.tools == ["default", "mcp__codegraph__*"]
        assert config.allowed_tools == ["Bash(git *)", "Edit"]
        assert config.disallowed_tools == ["Bash(rm *)"]
        assert config.mcp_configs == ["/etc/mcp/codegraph.json"]
        assert config.append_system_prompt == "Focus on Rust code"
        assert config.agent_name == "reviewer"
        assert "reviewer" in config.agents_json


class TestMergeAgentConfig:
    """Tests for _merge_agent_config helper."""

    def test_config_fields_only(self):
        config = SandboxConfig(tools=["default"], mcp_configs=["/a.json"])
        result = _merge_agent_config(config)
        assert result["tools"] == ["default"]
        assert result["mcp_configs"] == ["/a.json"]

    def test_subtask_overrides(self):
        config = SandboxConfig(tools=["default"])
        result = _merge_agent_config(config, {"tools": ["mcp__codegraph__*"]})
        assert result["tools"] == ["mcp__codegraph__*"]

    def test_subtask_adds_new_key(self):
        config = SandboxConfig(tools=["default"])
        result = _merge_agent_config(config, {"agent_name": "reviewer"})
        assert result["tools"] == ["default"]
        assert result["agent_name"] == "reviewer"

    def test_empty_subtask_no_override(self):
        config = SandboxConfig(tools=["default"])
        result = _merge_agent_config(config, {})
        assert result["tools"] == ["default"]

    def test_none_subtask_uses_config(self):
        config = SandboxConfig(tools=["default"])
        result = _merge_agent_config(config, None)
        assert result["tools"] == ["default"]


class TestClaudeCodeAdapterAgentFlags:
    """Tests for ClaudeCodeAdapter CLI flag generation."""

    def test_no_extra_flags_by_default(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(project_path="/tmp/project")
        request = adapter.build_request("Fix the bug", "/tmp/project", config)
        args = request["args"]
        assert "--tools" not in args
        assert "--mcp-config" not in args
        assert "--allowedTools" not in args
        assert "--disallowedTools" not in args
        assert "--append-system-prompt" not in args
        assert "--agent" not in args
        assert "--agents" not in args

    def test_tools_flag(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(
            project_path="/tmp/project",
            tools=["default", "mcp__codegraph__*"],
        )
        request = adapter.build_request("Fix", "/tmp/project", config)
        args = request["args"]
        idx = args.index("--tools")
        assert args[idx + 1] == "default"
        assert args[idx + 2] == "mcp__codegraph__*"

    def test_mcp_config_flag(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(
            project_path="/tmp/project",
            mcp_configs=["/etc/mcp/codegraph.json", "/etc/mcp/pencil.json"],
        )
        request = adapter.build_request("Fix", "/tmp/project", config)
        args = request["args"]
        idx = args.index("--mcp-config")
        assert args[idx + 1] == "/etc/mcp/codegraph.json"
        assert args[idx + 2] == "/etc/mcp/pencil.json"

    def test_allowed_tools_flag(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(
            project_path="/tmp/project",
            allowed_tools=["Bash(git *)", "Edit"],
        )
        request = adapter.build_request("Fix", "/tmp/project", config)
        args = request["args"]
        idx = args.index("--allowedTools")
        assert args[idx + 1] == "Bash(git *)"
        assert args[idx + 2] == "Edit"

    def test_disallowed_tools_flag(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(
            project_path="/tmp/project",
            disallowed_tools=["Bash(rm *)"],
        )
        request = adapter.build_request("Fix", "/tmp/project", config)
        args = request["args"]
        idx = args.index("--disallowedTools")
        assert args[idx + 1] == "Bash(rm *)"

    def test_append_system_prompt_flag(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(
            project_path="/tmp/project",
            append_system_prompt="Focus on Rust code",
        )
        request = adapter.build_request("Fix", "/tmp/project", config)
        args = request["args"]
        idx = args.index("--append-system-prompt")
        assert args[idx + 1] == "Focus on Rust code"

    def test_agent_name_flag(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(
            project_path="/tmp/project",
            agent_name="reviewer",
        )
        request = adapter.build_request("Fix", "/tmp/project", config)
        args = request["args"]
        idx = args.index("--agent")
        assert args[idx + 1] == "reviewer"

    def test_agents_json_flag(self):
        adapter = ClaudeCodeAdapter()
        agents = '{"reviewer": {"description": "Reviews code"}}'
        config = SandboxConfig(
            project_path="/tmp/project",
            agents_json=agents,
        )
        request = adapter.build_request("Fix", "/tmp/project", config)
        args = request["args"]
        idx = args.index("--agents")
        assert args[idx + 1] == agents

    def test_subtask_config_overrides(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(
            project_path="/tmp/project",
            tools=["default"],
        )
        request = adapter.build_request(
            "Fix", "/tmp/project", config,
            subtask_config={"tools": ["mcp__codegraph__*"], "agent_name": "reviewer"},
        )
        args = request["args"]
        # tools overridden by subtask config
        idx = args.index("--tools")
        assert args[idx + 1] == "mcp__codegraph__*"
        # agent_name from subtask config
        idx = args.index("--agent")
        assert args[idx + 1] == "reviewer"

    def test_all_flags_together(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(
            project_path="/tmp/project",
            tools=["default"],
            allowed_tools=["Bash(git *)"],
            mcp_configs=["/a.json"],
            append_system_prompt="Be thorough",
            agent_name="reviewer",
        )
        request = adapter.build_request("Fix", "/tmp/project", config)
        args = request["args"]
        assert "--tools" in args
        assert "--allowedTools" in args
        assert "--mcp-config" in args
        assert "--append-system-prompt" in args
        assert "--agent" in args


class TestSubtaskAgentConfig:
    """Tests for Subtask.agent_config field."""

    def test_default_agent_config_empty(self):
        st = Subtask(description="Fix bug")
        assert st.agent_config == {}

    def test_agent_config_with_tools(self):
        st = Subtask(
            description="Fix bug",
            agent_config={"tools": ["mcp__codegraph__*"], "agent_name": "reviewer"},
        )
        assert st.agent_config["tools"] == ["mcp__codegraph__*"]
        assert st.agent_config["agent_name"] == "reviewer"

    def test_task_round_trip_preserves_agent_config(self):
        st = Subtask(
            description="Fix bug",
            agent_config={"mcp_configs": ["/a.json"], "append_system_prompt": "Be safe"},
        )
        task = Task(description="Parent", subtasks=[st])
        data = task.to_dict()
        restored = Task.from_dict(data)
        assert restored.subtasks[0].agent_config["mcp_configs"] == ["/a.json"]
        assert restored.subtasks[0].agent_config["append_system_prompt"] == "Be safe"
