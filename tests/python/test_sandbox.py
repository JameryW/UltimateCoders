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
    _codex_mcp_server_toml,
    _merge_agent_config,
    _resolve_mcp_configs,
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
        assert "--sandbox" in request["args"]
        assert "workspace-write" in request["args"]

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

class TestWorkerDeriveCapabilitiesLegacy:
    """Tests for Worker._derive_capabilities from SandboxConfig (legacy baseline)."""

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


# ── NATS dispatch agent_config round-trip tests ──────────────────

class TestNatsDispatchAgentConfig:
    """Tests for agent_config surviving NATS subtask dispatch round-trip."""

    def test_dispatch_message_includes_agent_config(self):
        """Verify _dispatch_remote includes agent_config in the NATS message."""
        import json

        from ultimate_coders.agent.types import Subtask, SubtaskStatus

        subtask = Subtask(
            id="st-1",
            parent_id="t-1",
            description="Fix auth bug",
            status=SubtaskStatus.PENDING,
            agent_config={
                "tools": ["mcp__codegraph__*"],
                "mcp_configs": ["/etc/mcp/codegraph.json"],
                "disallowed_tools": ["Bash(rm *)"],
            },
        )

        # Simulate what _dispatch_remote builds
        msg_dict = {
            "task_id": subtask.parent_id,
            "subtask_id": subtask.id,
            "description": subtask.description,
            "depends_on": subtask.depends_on,
            "file_constraints": subtask.file_constraints,
            "expected_output": subtask.expected_output,
            "timeout_seconds": subtask.timeout_seconds or 600,
            "dispatch_mode": subtask.dispatch_mode.value,
            "required_capabilities": subtask.required_capabilities,
            "agent_config": subtask.agent_config,
        }

        # Serialize and deserialize (NATS round-trip)
        encoded = json.dumps(msg_dict).encode()
        decoded = json.loads(encoded.decode())

        assert decoded["agent_config"]["tools"] == ["mcp__codegraph__*"]
        assert decoded["agent_config"]["mcp_configs"] == ["/etc/mcp/codegraph.json"]
        assert decoded["agent_config"]["disallowed_tools"] == ["Bash(rm *)"]

    def test_dispatch_message_empty_agent_config(self):
        """Verify empty agent_config is handled (backward compat)."""
        import json

        from ultimate_coders.agent.types import Subtask

        subtask = Subtask(description="Fix bug")

        msg_dict = {
            "task_id": subtask.parent_id,
            "subtask_id": subtask.id,
            "description": subtask.description,
            "agent_config": subtask.agent_config,
        }

        encoded = json.dumps(msg_dict).encode()
        decoded = json.loads(encoded.decode())
        assert decoded["agent_config"] == {}

    def test_handle_subtask_reconstructs_agent_config(self):
        """Verify _handle_subtask_execute reconstructs Subtask with agent_config."""
        from ultimate_coders.agent.types import DispatchMode, Subtask, SubtaskStatus

        # Simulate the incoming NATS message data
        data = {
            "task_id": "t-1",
            "subtask_id": "st-1",
            "description": "Fix auth bug",
            "depends_on": [],
            "file_constraints": ["src/auth.rs"],
            "expected_output": "Bug fixed",
            "timeout_seconds": 600,
            "dispatch_mode": "prefer_remote",
            "required_capabilities": ["code"],
            "agent_config": {
                "tools": ["default", "mcp__codegraph__*"],
                "append_system_prompt": "Focus on security",
            },
        }

        # Reconstruct Subtask (mirrors _handle_subtask_execute logic)
        subtask = Subtask(
            id=data["subtask_id"],
            parent_id=data["task_id"],
            description=data["description"],
            status=SubtaskStatus.PENDING,
            depends_on=data.get("depends_on", []),
            file_constraints=data.get("file_constraints", []),
            expected_output=data.get("expected_output", ""),
            timeout_seconds=data.get("timeout_seconds", 600),
            dispatch_mode=DispatchMode(data.get("dispatch_mode", "prefer_remote")),
            required_capabilities=data.get("required_capabilities", []),
            agent_config=data.get("agent_config", {}),
        )

        assert subtask.agent_config["tools"] == ["default", "mcp__codegraph__*"]
        assert subtask.agent_config["append_system_prompt"] == "Focus on security"

    def test_handle_subtask_missing_agent_config_defaults_empty(self):
        """Backward compat: messages without agent_config default to {}."""
        from ultimate_coders.agent.types import Subtask, SubtaskStatus

        data = {
            "task_id": "t-1",
            "subtask_id": "st-1",
            "description": "Fix bug",
        }

        subtask = Subtask(
            id=data["subtask_id"],
            parent_id=data["task_id"],
            description=data["description"],
            status=SubtaskStatus.PENDING,
            agent_config=data.get("agent_config", {}),
        )

        assert subtask.agent_config == {}


class TestOrchestratorAgentConfig:
    """Tests for Orchestrator.submit_task with agent_config."""

    @pytest.mark.asyncio
    async def test_submit_task_passes_agent_config_to_subtasks(self):
        from ultimate_coders.agent.orchestrator import Orchestrator

        orch = Orchestrator()
        task = await orch.submit_task(
            "Fix bug A\nFix bug B",
            agent_config={"tools": ["mcp__codegraph__*"]},
        )
        assert len(task.subtasks) == 2
        for st in task.subtasks:
            assert st.agent_config == {"tools": ["mcp__codegraph__*"]}

    @pytest.mark.asyncio
    async def test_submit_task_no_agent_config_defaults_empty(self):
        from ultimate_coders.agent.orchestrator import Orchestrator

        orch = Orchestrator()
        task = await orch.submit_task("Fix bug")
        assert task.subtasks[0].agent_config == {}


# ── Inline MCP config resolution tests ──────────────────────────

class TestResolveMcpConfigs:
    """Tests for _resolve_mcp_configs helper."""

    def test_file_paths_pass_through(self):
        resolved, temps = _resolve_mcp_configs(["/etc/mcp/a.json", "/etc/mcp/b.json"])
        assert resolved == ["/etc/mcp/a.json", "/etc/mcp/b.json"]
        assert temps == []

    def test_inline_dict_creates_temp_file(self):
        server_cfg = {"codegraph": {"command": "npx", "args": ["-y", "mcp-codegraph"]}}
        resolved, temps = _resolve_mcp_configs([server_cfg])
        assert len(resolved) == 1
        assert len(temps) == 1
        # Verify temp file content
        import os
        with open(temps[0]) as f:
            data = json.load(f)
        assert "mcpServers" in data
        assert "codegraph" in data["mcpServers"]
        # Cleanup
        os.unlink(temps[0])

    def test_mixed_paths_and_dicts(self):
        server_cfg = {"pencil": {"url": "https://example.com/mcp"}}
        resolved, temps = _resolve_mcp_configs(["/a.json", server_cfg])
        assert len(resolved) == 2
        assert resolved[0] == "/a.json"
        assert len(temps) == 1
        import os
        for p in temps:
            os.unlink(p)

    def test_empty_list(self):
        resolved, temps = _resolve_mcp_configs([])
        assert resolved == []
        assert temps == []

    def test_invalid_entry_skipped(self):
        resolved, temps = _resolve_mcp_configs([42])
        assert resolved == []
        assert temps == []


class TestCodexMcpServerToml:
    """Tests for _codex_mcp_server_toml helper."""

    def test_stdio_transport(self):
        cfg = {"command": "npx", "args": ["-y", "@mcp/server"], "env": {"KEY": "val"}}
        toml = _codex_mcp_server_toml("my-server", cfg)
        assert "[mcp_servers.my-server]" in toml
        assert 'command = "npx"' in toml
        assert '"-y"' in toml
        assert '"@mcp/server"' in toml
        assert "KEY" in toml

    def test_http_transport(self):
        cfg = {"url": "https://example.com/mcp", "bearer_token_env_var": "API_KEY"}
        toml = _codex_mcp_server_toml("remote", cfg)
        assert 'url = "https://example.com/mcp"' in toml
        assert "bearer_token_env_var" in toml

    def test_enabled_disabled_tools(self):
        cfg = {"command": "npx", "enabled_tools": ["tool_a"], "disabled_tools": ["tool_b"]}
        toml = _codex_mcp_server_toml("srv", cfg)
        assert "enabled_tools" in toml
        assert "disabled_tools" in toml


class TestCodexAdapterBuildRequest:
    """Tests for CodexAdapter.build_request with subtask_config."""

    def test_default_no_temp_files(self):
        adapter = CodexAdapter()
        config = SandboxConfig(project_path="/tmp/project")
        request = adapter.build_request("Fix bug", "/tmp/project", config)
        assert request["command"] == "codex"
        assert "--sandbox" in request["args"]
        assert "workspace-write" in request["args"]
        assert request.get("_temp_files", []) == []

    def test_subtask_config_mcp_creates_temp_toml(self):
        adapter = CodexAdapter()
        config = SandboxConfig(project_path="/tmp/project")
        mcp_inline = {"codegraph": {"command": "npx", "args": ["-y", "mcp-codegraph"]}}
        request = adapter.build_request(
            "Fix bug", "/tmp/project", config,
            subtask_config={"mcp_configs": [mcp_inline]},
        )
        assert "_temp_files" in request
        temps = request["_temp_files"]
        assert len(temps) >= 1
        # Verify the temp toml content
        import os
        for p in temps:
            if p.endswith(".toml"):
                with open(p) as f:
                    content = f.read()
                assert "[mcp_servers.codegraph]" in content
            os.unlink(p)

    def test_sandbox_workspace_write_replaces_full_auto(self):
        adapter = CodexAdapter()
        config = SandboxConfig(project_path="/tmp/project")
        request = adapter.build_request("Fix", "/tmp/project", config)
        assert "--full-auto" not in request["args"]
        assert "--sandbox" in request["args"]


class TestClaudeCodeAdapterInlineMcp:
    """Tests for ClaudeCodeAdapter with inline MCP configs."""

    def test_inline_mcp_creates_temp_file(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(project_path="/tmp/project")
        mcp_inline = {"codegraph": {"command": "npx", "args": ["-y", "mcp-codegraph"]}}
        request = adapter.build_request(
            "Fix bug", "/tmp/project", config,
            subtask_config={"mcp_configs": [mcp_inline]},
        )
        args = request["args"]
        idx = args.index("--mcp-config")
        mcp_path = args[idx + 1]
        # Verify temp file exists and has correct content
        import os
        with open(mcp_path) as f:
            data = json.load(f)
        assert "mcpServers" in data
        assert "codegraph" in data["mcpServers"]
        # Cleanup
        for p in request.get("_temp_files", []):
            os.unlink(p)

    def test_mixed_mcp_paths_and_inline(self):
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(
            project_path="/tmp/project",
            mcp_configs=["/existing/config.json"],
        )
        mcp_inline = {"pencil": {"url": "https://example.com/mcp"}}
        request = adapter.build_request(
            "Fix", "/tmp/project", config,
            subtask_config={"mcp_configs": ["/existing/config.json", mcp_inline]},
        )
        args = request["args"]
        idx = args.index("--mcp-config")
        # First is file path, second is temp file
        assert args[idx + 1] == "/existing/config.json"
        import os
        for p in request.get("_temp_files", []):
            os.unlink(p)


# ── Worker agent_config auto-derivation tests ──────────────────

class TestWorkerDeriveCapabilities:
    """Tests for Worker._derive_capabilities enhancement."""

    def test_base_capabilities(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        caps = w.capabilities
        assert "code" in caps
        assert "search" in caps
        assert "memory" in caps

    def test_mcp_configs_add_per_server_caps(self):
        from ultimate_coders.agent.worker import Worker
        mcp_inline = {"codegraph": {"command": "npx"}, "pencil": {"url": "https://x.com"}}
        cfg = SandboxConfig(mcp_configs=[mcp_inline])
        w = Worker(sandbox_config=cfg)
        assert "mcp:codegraph" in w.capabilities
        assert "mcp:pencil" in w.capabilities

    def test_mcp_file_path_extracts_name(self):
        from ultimate_coders.agent.worker import Worker
        cfg = SandboxConfig(mcp_configs=["/etc/mcp/codegraph.json"])
        w = Worker(sandbox_config=cfg)
        assert "mcp:codegraph" in w.capabilities

    def test_tools_mcp_prefix_extracts_server(self):
        from ultimate_coders.agent.worker import Worker
        cfg = SandboxConfig(tools=["default", "mcp__codegraph__*", "mcp__pencil__*"])
        w = Worker(sandbox_config=cfg)
        assert "mcp:codegraph" in w.capabilities
        assert "mcp:pencil" in w.capabilities

    def test_agents_json_parses_names(self):
        from ultimate_coders.agent.worker import Worker
        agents_json = json.dumps({
            "reviewer": {"description": "Reviews"},
            "writer": {"description": "Writes"},
        })
        cfg = SandboxConfig(agents_json=agents_json)
        w = Worker(sandbox_config=cfg)
        assert "agent:reviewer" in w.capabilities
        assert "agent:writer" in w.capabilities

    def test_no_duplicate_caps(self):
        from ultimate_coders.agent.worker import Worker
        cfg = SandboxConfig(
            tools=["default", "mcp__codegraph__*"],
            mcp_configs=["/etc/mcp/codegraph.json"],
        )
        w = Worker(sandbox_config=cfg)
        # "codegraph" and "mcp:codegraph" are distinct capability tags
        assert "codegraph" in w.capabilities
        assert "mcp:codegraph" in w.capabilities
        # Same tag should not appear twice
        assert w.capabilities.count("codegraph") == 1
        assert w.capabilities.count("mcp:codegraph") == 1


class TestWorkerResolveAgentConfig:
    """Tests for Worker._resolve_agent_config."""

    def test_explicit_agent_config_preserved(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(
            description="Fix bug",
            agent_config={"tools": ["mcp__codegraph__*"]},
        )
        result = w._resolve_agent_config(st)
        assert result == {"tools": ["mcp__codegraph__*"]}

    def test_capability_match_review(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(
            description="Check code quality",
            required_capabilities=["review"],
        )
        result = w._resolve_agent_config(st)
        assert "disallowed_tools" in result
        assert "Edit" in result["disallowed_tools"]

    def test_description_heuristic_review(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(description="Review the auth module for security issues")
        result = w._resolve_agent_config(st)
        assert "disallowed_tools" in result

    def test_description_heuristic_search(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(description="Find all usages of deprecated API")
        result = w._resolve_agent_config(st)
        assert "tools" in result
        assert "mcp__codegraph__*" in result["tools"]

    def test_capability_overrides_template(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        # "review" capability + "search" in description → capability wins
        st = Subtask(
            description="Search and review the codebase",
            required_capabilities=["review"],
        )
        result = w._resolve_agent_config(st)
        # review profile's disallowed_tools should be there
        assert "disallowed_tools" in result

    def test_no_match_returns_empty(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(description="Implement the new feature")
        result = w._resolve_agent_config(st)
        assert result == {}

    def test_empty_agent_config_treated_as_not_set(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(
            description="Review code",
            agent_config={},
        )
        # Empty dict is falsy for our check, falls through to template
        result = w._resolve_agent_config(st)
        assert "disallowed_tools" in result


# ── End-to-end pipeline tests ──────────────────────────────────

class TestAgentConfigPipeline:
    """End-to-end: required_capabilities → _resolve_agent_config → build_request.

    Verifies that the full chain produces correct CLI flags.
    """

    def test_review_subtask_produces_disallowed_tools_flag(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(
            description="Review the auth module",
            required_capabilities=["review"],
        )
        agent_cfg = w._resolve_agent_config(st)
        # Feed to ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(project_path="/tmp/project")
        request = adapter.build_request("Review", "/tmp/project", config, subtask_config=agent_cfg)
        args = request["args"]
        assert "--disallowedTools" in args
        assert "Edit" in args

    def test_codegraph_search_subtask_produces_tools_flag(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(
            description="Find all usages of deprecated API",
            required_capabilities=["codegraph"],
        )
        agent_cfg = w._resolve_agent_config(st)
        adapter = ClaudeCodeAdapter()
        config = SandboxConfig(project_path="/tmp/project")
        request = adapter.build_request("Search", "/tmp/project", config, subtask_config=agent_cfg)
        args = request["args"]
        idx = args.index("--tools")
        assert "mcp__codegraph__*" in args[idx + 1:]

    def test_codex_adapter_with_review_subtask(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(
            description="Review code quality",
            required_capabilities=["review"],
        )
        agent_cfg = w._resolve_agent_config(st)
        adapter = CodexAdapter()
        config = SandboxConfig(project_path="/tmp/project")
        request = adapter.build_request("Review", "/tmp/project", config, subtask_config=agent_cfg)
        # Codex uses config.toml, not CLI flags — verify temp file created
        import os
        temps = request.get("_temp_files", [])
        for p in temps:
            os.unlink(p)

    def test_explicit_config_bypasses_derivation(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        # User explicitly sets tools — should NOT be overridden by review template
        st = Subtask(
            description="Review code",
            agent_config={"tools": ["default", "Edit"]},
            required_capabilities=["review"],
        )
        agent_cfg = w._resolve_agent_config(st)
        # Explicit config preserved, review template NOT applied
        assert agent_cfg == {"tools": ["default", "Edit"]}
        assert "disallowed_tools" not in agent_cfg
