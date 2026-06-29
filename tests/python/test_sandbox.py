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
        assert '[mcp_servers."my-server"]' in toml
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
        # Verify the temp config.toml content and naming
        import os
        for p in temps:
            if p.endswith(".config.toml"):
                # Profile file must be named <name>.config.toml
                basename = os.path.basename(p)
                assert basename.endswith(".config.toml")
                with open(p) as f:
                    content = f.read()
                assert '[mcp_servers."codegraph"]' in content
                # --profile arg should be the stem (without .config.toml)
                profile_name = basename[: -len(".config.toml")]
                assert "--profile" in request["args"]
                profile_idx = request["args"].index("--profile")
                assert request["args"][profile_idx + 1] == profile_name
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
        # Codex uses config.profile, not CLI flags for tool restrictions
        # Review profile has disallowed_tools which Codex logs as unsupported
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


# ── Retry & Progress tests ──────────────────────────────────────


class TestWorkerRetry:
    """Tests for subtask retry logic."""

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """Failed subtask retries up to MAX_RETRIES times."""
        from unittest.mock import AsyncMock, patch

        from ultimate_coders.agent.worker import Worker

        w = Worker()

        call_count = 0
        async def mock_execute(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            return AgentOutput(summary="fail", success=False)

        with patch.object(w._sandbox_manager, "execute", side_effect=mock_execute):
            with patch.object(w, "_publish_event", new_callable=AsyncMock):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    subtask = Subtask(id="s1", parent_id="t1", description="fix bug")
                    result = await w.execute_subtask(subtask)

        assert result.success is False
        assert call_count == 3  # MAX_RETRIES
        assert result.retry_count == 2  # retried twice

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self):
        """Subtask succeeds on retry."""
        from unittest.mock import AsyncMock, patch

        from ultimate_coders.agent.worker import Worker

        w = Worker()
        call_count = 0
        async def mock_execute(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AgentOutput(summary="fail", success=False)
            return AgentOutput(summary="done", success=True)

        with patch.object(w._sandbox_manager, "execute", side_effect=mock_execute):
            with patch.object(w, "_publish_event", new_callable=AsyncMock):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    subtask = Subtask(id="s2", parent_id="t1", description="fix bug")
                    result = await w.execute_subtask(subtask)

        assert result.success is True
        assert call_count == 2
        assert result.retry_count == 1

    @pytest.mark.asyncio
    async def test_no_retry_on_success(self):
        """Successful subtask is not retried."""
        from unittest.mock import AsyncMock, patch

        from ultimate_coders.agent.worker import Worker

        w = Worker()
        call_count = 0
        async def mock_execute(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            return AgentOutput(summary="done", success=True)

        with patch.object(w._sandbox_manager, "execute", side_effect=mock_execute):
            with patch.object(w, "_publish_event", new_callable=AsyncMock):
                subtask = Subtask(id="s3", parent_id="t1", description="fix bug")
                result = await w.execute_subtask(subtask)

        assert result.success is True
        assert call_count == 1
        assert result.retry_count == 0


class TestWorkerProgress:
    """Tests for subtask progress events."""

    @pytest.mark.asyncio
    async def test_progress_events_emitted(self):
        """Progress events are emitted at key phases."""
        from unittest.mock import patch

        from ultimate_coders.agent.worker import Worker

        w = Worker()
        events = []

        async def mock_publish(event_type, **kwargs):
            events.append((event_type, kwargs.get("data", {})))

        async def mock_execute(prompt, **kwargs):
            return AgentOutput(summary="done", success=True)

        with patch.object(w._sandbox_manager, "execute", side_effect=mock_execute):
            with patch.object(w, "_publish_event", side_effect=mock_publish):
                subtask = Subtask(id="s4", parent_id="t1", description="fix bug")
                await w.execute_subtask(subtask)

        progress_events = [(e, d) for e, d in events if e == "subtask_progress"]
        phases = [d.get("phase") for _, d in progress_events]
        assert "preparing" in phases
        assert "executing" in phases
        assert "validating" in phases
        assert "finalizing" in phases


class TestNewTemplates:
    """Tests for expanded AGENT_PROFILES and SUBTASK_TEMPLATES."""

    def test_fix_template(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(description="Fix the null pointer exception in auth")
        result = w._resolve_agent_config(st)
        assert "append_system_prompt" in result
        assert "minimal" in result["append_system_prompt"].lower()

    def test_test_template(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(description="Write unit tests for the parser")
        result = w._resolve_agent_config(st)
        assert "append_system_prompt" in result

    def test_refactor_template(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(description="Refactor the database connection pool")
        result = w._resolve_agent_config(st)
        assert "append_system_prompt" in result
        assert "tools" in result
        assert "mcp__codegraph__*" in result["tools"]

    def test_deploy_template(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(description="Deploy to staging environment")
        result = w._resolve_agent_config(st)
        assert "disallowed_tools" in result

    def test_docs_template(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(description="Document the API endpoints")
        result = w._resolve_agent_config(st)
        assert "append_system_prompt" in result

    def test_fix_capability_profile(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(required_capabilities=["fix"])
        result = w._resolve_agent_config(st)
        assert "append_system_prompt" in result

    def test_test_capability_profile(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(required_capabilities=["test"])
        result = w._resolve_agent_config(st)
        assert "append_system_prompt" in result

    def test_refactor_capability_profile(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(required_capabilities=["refactor"])
        result = w._resolve_agent_config(st)
        assert "tools" in result

    def test_deploy_capability_profile(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(required_capabilities=["deploy"])
        result = w._resolve_agent_config(st)
        assert "disallowed_tools" in result

    def test_docs_capability_profile(self):
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(required_capabilities=["docs"])
        result = w._resolve_agent_config(st)
        assert "append_system_prompt" in result


class TestProjectIdPropagation:
    """Integration tests for project_id propagation through dispatch → search → prompt."""

    @pytest.mark.asyncio
    async def test_subtask_project_id_reaches_orchestrator(self):
        """Orchestrator passes project_id to created Subtask objects."""
        from ultimate_coders.agent.orchestrator import Orchestrator
        orch = Orchestrator()
        task = await orch.submit_task("fix auth\nadd tests", project_id="my-project")
        assert task.project_id == "my-project"
        for st in task.subtasks:
            assert st.project_id == "my-project"

    @pytest.mark.asyncio
    async def test_search_context_injection_with_mock_engine(self):
        """_build_search_context injects search results when engine returns data."""
        from unittest.mock import MagicMock

        from ultimate_coders.agent.worker import Worker

        engine = MagicMock()
        item = MagicMock()
        item.repo_id = "backend"
        item.file_path = "src/auth.py"
        item.content_snippet = "def authenticate(user, pwd):"
        result = MagicMock()
        result.items = [item]
        engine.search.return_value = result
        repo = MagicMock()
        repo.repo_id = "backend"
        engine.list_repos.return_value = [repo]

        w = Worker(engine=engine)
        st = Subtask(id="s1", description="fix auth", project_id="backend")
        ctx = w._build_search_context(st)
        assert ctx is not None
        assert "backend" in ctx
        assert "src/auth.py" in ctx

    @pytest.mark.asyncio
    async def test_search_context_enriches_subtask(self):
        """_build_search_context produces formatted context with search results."""
        from unittest.mock import MagicMock

        from ultimate_coders.agent.worker import Worker

        engine = MagicMock()
        item = MagicMock()
        item.repo_id = "backend"
        item.file_path = "src/auth.py"
        item.content_snippet = "def authenticate():"
        result = MagicMock()
        result.items = [item]
        engine.search.return_value = result
        repo = MagicMock()
        repo.repo_id = "backend"
        engine.list_repos.return_value = [repo]

        w = Worker(engine=engine)
        st = Subtask(id="s1", parent_id="t1", description="fix auth", project_id="backend")

        # Verify search context is generated
        ctx = w._build_search_context(st)
        assert ctx is not None
        assert "backend" in ctx
        assert "src/auth.py" in ctx
        assert "authenticate" in ctx


class TestSearchCache:
    """Tests for WorkerLocalCache LRU + TTL search caching."""

    def test_cache_miss_returns_none(self):
        from ultimate_coders.agent.search_cache import WorkerLocalCache
        cache = WorkerLocalCache()
        assert cache.get_search("nonexistent") is None

    def test_cache_put_and_get(self):
        from ultimate_coders.agent.search_cache import WorkerLocalCache
        cache = WorkerLocalCache()
        cache.put_search("key1", ["result1"])
        assert cache.get_search("key1") == ["result1"]

    def test_cache_key_deterministic(self):
        from ultimate_coders.agent.search_cache import WorkerLocalCache
        key1 = WorkerLocalCache.search_key("auth", ["backend"], ["hybrid"], 10)
        key2 = WorkerLocalCache.search_key("auth", ["backend"], ["hybrid"], 10)
        assert key1 == key2

    def test_cache_key_different_queries(self):
        from ultimate_coders.agent.search_cache import WorkerLocalCache
        key1 = WorkerLocalCache.search_key("auth", ["backend"], ["hybrid"], 10)
        key2 = WorkerLocalCache.search_key("search", ["backend"], ["hybrid"], 10)
        assert key1 != key2

    def test_repo_cache(self):
        from ultimate_coders.agent.search_cache import WorkerLocalCache
        cache = WorkerLocalCache()
        assert cache.get_repos() is None
        cache.put_repos([{"repo_id": "backend"}])
        result = cache.get_repos()
        assert result is not None
        assert len(result) == 1

    def test_cache_invalidate(self):
        from ultimate_coders.agent.search_cache import WorkerLocalCache
        cache = WorkerLocalCache()
        cache.put_search("key1", "val1")
        cache.put_search("key2", "val2")
        cache.invalidate()
        assert cache.get_search("key1") is None
        assert cache.get_search("key2") is None

    def test_cache_lru_eviction(self):
        from ultimate_coders.agent.search_cache import WorkerLocalCache
        cache = WorkerLocalCache(max_search_entries=3)
        cache.put_search("a", 1)
        cache.put_search("b", 2)
        cache.put_search("c", 3)
        cache.put_search("d", 4)  # evicts "a"
        assert cache.get_search("a") is None
        assert cache.get_search("d") == 4


class TestCrossRepoSearchAndMemorySharing:
    """Tests for cross-repo search context injection and memory sharing."""

    def test_subtask_has_project_id(self):
        """Subtask dataclass includes project_id field."""
        st = Subtask(id="s1", parent_id="t1", description="fix auth", project_id="my-project")
        assert st.project_id == "my-project"

    def test_subtask_project_id_default_empty(self):
        """project_id defaults to empty string (backward compatible)."""
        st = Subtask(id="s2", parent_id="t2", description="fix bug")
        assert st.project_id == ""

    def test_search_across_repos_no_engine(self):
        """search_across_repos returns None when engine is unavailable."""
        from ultimate_coders.agent.worker import Worker
        w = Worker(engine=None)
        result = w.search_across_repos("authentication")
        assert result is None

    def test_build_search_context_no_engine(self):
        """_build_search_context returns None when engine is unavailable."""
        from ultimate_coders.agent.worker import Worker
        w = Worker(engine=None)
        st = Subtask(id="s1", description="fix auth")
        result = w._build_search_context(st)
        assert result is None

    def test_build_search_context_no_description(self):
        """_build_search_context returns None for empty description."""
        from ultimate_coders.agent.worker import Worker
        w = Worker()
        st = Subtask(id="s1", description="")
        result = w._build_search_context(st)
        assert result is None

    def test_read_shared_memory_no_engine(self):
        """read_shared_memory returns None when engine is unavailable."""
        from ultimate_coders.agent.worker import Worker
        w = Worker(engine=None)
        result = w.read_shared_memory("architecture")
        assert result is None

    def test_write_shared_memory_no_engine(self):
        """write_shared_memory returns None when engine is unavailable."""
        from ultimate_coders.agent.worker import Worker
        w = Worker(engine=None)
        result = w.write_shared_memory("architecture", "Use microservices")
        assert result is None

    def test_delete_shared_memory_no_engine(self):
        """delete_shared_memory returns False when engine is unavailable."""
        from ultimate_coders.agent.worker import Worker
        w = Worker(engine=None)
        assert w.delete_shared_memory("architecture") is False

    def test_search_query_in_all_repos(self):
        """SearchQuery.in_all_repos() populates repo_ids from engine."""
        from unittest.mock import MagicMock

        from ultimate_coders.search.query import SearchQuery

        engine = MagicMock()
        repo1 = MagicMock()
        repo1.repo_id = "backend"
        repo2 = MagicMock()
        repo2.repo_id = "frontend"
        engine.list_repos.return_value = [repo1, repo2]

        sq = SearchQuery("auth").in_all_repos(engine)
        d = sq.to_dict()
        assert d["repo_ids"] == ["backend", "frontend"]

    def test_search_query_in_all_repos_failure(self):
        """SearchQuery.in_all_repos() gracefully handles engine failure."""
        from unittest.mock import MagicMock

        from ultimate_coders.search.query import SearchQuery

        engine = MagicMock()
        engine.list_repos.side_effect = RuntimeError("connection failed")

        sq = SearchQuery("auth").in_all_repos(engine)
        d = sq.to_dict()
        assert d["repo_ids"] == []  # graceful degradation


class TestCrossRepoSearchAndSharedMemory:
    """Forward-path coverage for cross-repo search cache + shared memory.

    The no-engine degradation paths are covered above; these exercise the
    real Engine-backed paths: search-cache hit/miss, shared-memory read, and
    the NATS broadcast on shared-memory write.
    """

    def _make_worker(self, engine=None, nats_publisher=None):
        from ultimate_coders.agent.search_cache import WorkerLocalCache
        from ultimate_coders.agent.worker import Worker

        w = Worker(engine=engine, nats_publisher=nats_publisher)
        # ponytail: default cache is a process singleton — give each test an
        # isolated instance so cached entries don't leak across tests.
        w._search_cache = WorkerLocalCache()
        return w

    def test_build_search_context_caches_on_miss(self):
        """First search calls engine.search and caches the result."""
        from unittest.mock import MagicMock

        engine = MagicMock()
        engine.list_repos.return_value = []
        item = MagicMock(repo_id="r1", file_path="a.py", content_snippet="x")
        engine.search.return_value = MagicMock(items=[item])

        w = self._make_worker(engine=engine)
        st = Subtask(id="s1", description="auth logic", project_id="r1")
        ctx = w._build_search_context(st)

        assert ctx is not None
        assert "Related code" in ctx
        engine.search.assert_called_once()

    def test_build_search_context_hits_cache(self):
        """Second identical search is served from cache — engine not called again."""
        from unittest.mock import MagicMock

        engine = MagicMock()
        engine.list_repos.return_value = []
        item = MagicMock(repo_id="r1", file_path="a.py", content_snippet="x")
        engine.search.return_value = MagicMock(items=[item])

        w = self._make_worker(engine=engine)
        st = Subtask(id="s1", description="auth logic", project_id="r1")
        w._build_search_context(st)
        w._build_search_context(st)  # second call

        engine.search.assert_called_once()  # cache hit on 2nd

    def test_read_shared_memory_calls_engine(self):
        """read_shared_memory routes to engine.read_memory with project scope."""
        from unittest.mock import MagicMock

        engine = MagicMock()
        engine.read_memory.return_value = MagicMock(content="Use PostgreSQL")
        w = self._make_worker(engine=engine)

        result = w.read_shared_memory("decisions", project_id="proj-1")

        assert result is not None
        assert result.content == "Use PostgreSQL"
        engine.read_memory.assert_called_once()
        _, kwargs = engine.read_memory.call_args
        assert kwargs["key_scope"] == "project"
        assert kwargs["project_id"] == "proj-1"

    def test_write_shared_memory_broadcasts_via_nats(self):
        """write_shared_memory publishes uc.memory.changed when a publisher is set."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        engine = MagicMock()
        engine.write_memory.return_value = MagicMock(content="data")

        publisher = MagicMock()
        publisher.publish_memory_changed = AsyncMock()

        w = self._make_worker(engine=engine, nats_publisher=publisher)
        w.worker_id = "worker-A"

        # Run inside an event loop so the fire-and-forget task can schedule.
        async def _drive():
            result = w.write_shared_memory("k", "v", project_id="proj-1")
            # Yield once so the scheduled create_task coroutine runs.
            await asyncio.sleep(0)
            return result

        result = asyncio.run(_drive())

        assert result is not None
        engine.write_memory.assert_called_once()
        publisher.publish_memory_changed.assert_awaited_once_with(
            project_id="proj-1", key="k", action="write", source_worker="worker-A",
        )

    def test_delete_shared_memory_broadcasts_via_nats(self):
        """delete_shared_memory publishes uc.memory.changed with action='delete'."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        engine = MagicMock()  # delete_memory returns None, no exception = success
        publisher = MagicMock()
        publisher.publish_memory_changed = AsyncMock()

        w = self._make_worker(engine=engine, nats_publisher=publisher)
        w.worker_id = "worker-A"

        async def _drive():
            ok = w.delete_shared_memory("k", project_id="proj-1")
            await asyncio.sleep(0)  # let the fire-and-forget task run
            return ok

        ok = asyncio.run(_drive())

        assert ok is True
        engine.delete_memory.assert_called_once_with(
            key_scope="project", key="k", project_id="proj-1",
        )
        publisher.publish_memory_changed.assert_awaited_once_with(
            project_id="proj-1", key="k", action="delete", source_worker="worker-A",
        )

    def test_delete_shared_memory_failure_skips_broadcast(self):
        """If engine.delete_memory raises, no broadcast fires and returns False."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        engine = MagicMock()
        engine.delete_memory.side_effect = RuntimeError("storage down")
        publisher = MagicMock()
        publisher.publish_memory_changed = AsyncMock()

        w = self._make_worker(engine=engine, nats_publisher=publisher)
        w.worker_id = "worker-A"

        async def _drive():
            ok = w.delete_shared_memory("k", project_id="proj-1")
            await asyncio.sleep(0)
            return ok

        ok = asyncio.run(_drive())

        assert ok is False
        engine.delete_memory.assert_called_once()
        publisher.publish_memory_changed.assert_not_awaited()

    def test_handle_memory_changed_invalidates_on_other_worker(self):
        """Receiving another Worker's broadcast invalidates the local cache."""
        import asyncio
        import json
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from ultimate_coders.nats_worker import NatsWorker

        nw = NatsWorker()
        worker = MagicMock()
        worker.worker_id = "worker-A"
        cache = MagicMock()
        worker._search_cache = cache
        nw._worker = worker

        msg = SimpleNamespace(data=json.dumps({
            "project_id": "proj-1", "key": "k",
            "action": "write", "source_worker": "worker-B",
        }).encode())

        asyncio.run(nw._handle_memory_changed(msg))

        cache.invalidate.assert_called_once()

    def test_handle_memory_changed_skips_own_broadcast(self):
        """A Worker must not invalidate on its own broadcast."""
        import asyncio
        import json
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from ultimate_coders.nats_worker import NatsWorker

        nw = NatsWorker()
        worker = MagicMock()
        worker.worker_id = "worker-A"
        cache = MagicMock()
        worker._search_cache = cache
        nw._worker = worker

        msg = SimpleNamespace(data=json.dumps({
            "project_id": "proj-1", "key": "k",
            "action": "write", "source_worker": "worker-A",  # same as worker
        }).encode())

        asyncio.run(nw._handle_memory_changed(msg))

        cache.invalidate.assert_not_called()

    def test_handle_memory_changed_bad_payload_no_crash(self):
        """Malformed payload must not raise nor invalidate."""
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from ultimate_coders.nats_worker import NatsWorker

        nw = NatsWorker()
        worker = MagicMock()
        worker.worker_id = "worker-A"
        cache = MagicMock()
        worker._search_cache = cache
        nw._worker = worker

        msg = SimpleNamespace(data=b"not-json")

        asyncio.run(nw._handle_memory_changed(msg))  # must not raise

        cache.invalidate.assert_not_called()
