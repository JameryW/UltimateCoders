# PRD: Enhance Sandbox Worker Capabilities — Skill / MCP / Tool Support

## Problem

Sandbox workers currently invoke `claude -p <prompt> --output-format stream-json --max-turns 20 --dangerously-skip-permissions` with zero ability to customize the agent's available tools, skills, or MCP servers. This means:

1. Every subtask runs with the **same** default toolset — no way to grant/restrict specific tools per subtask
2. No MCP server integration — workers can't use project-specific MCP tools (e.g., codegraph, pencil, context7)
3. No skill support — can't invoke custom slash commands or skill-specific agent behaviors
4. No system prompt customization — can't steer agent personality or constraints per subtask
5. Worker `capabilities` list is static strings like `["code", "search", "memory", "test"]` — not connected to actual tool availability

## Solution

Extend `SandboxConfig` + `Subtask` + `ClaudeCodeAdapter` to pass claude CLI flags: `--tools`, `--mcp-config`, `--allowedTools`, `--disallowedTools`, `--append-system-prompt`, `--agent`. The subtask carries the config; the adapter builds the right CLI args.

### Key Changes

#### 1. `SandboxConfig` — add tool/skill/mcp fields (Python)

```python
@dataclass
class SandboxConfig:
    # ... existing fields ...
    # NEW:
    tools: list[str] | None = None           # --tools (e.g., ["default", "mcp__codegraph__*"])
    allowed_tools: list[str] | None = None   # --allowedTools
    disallowed_tools: list[str] | None = None # --disallowedTools
    mcp_configs: list[str] | None = None     # --mcp-config paths
    append_system_prompt: str | None = None   # --append-system-prompt
    agent_name: str | None = None             # --agent (custom agent name)
    agents_json: str | None = None            # --agents JSON string
```

#### 2. `Subtask` — add `agent_config` field

```python
@dataclass
class Subtask:
    # ... existing fields ...
    # NEW: per-subtask agent configuration overrides
    agent_config: dict[str, Any] = field(default_factory=dict)
    # Keys: tools, allowed_tools, disallowed_tools, mcp_configs,
    #       append_system_prompt, agent_name, agents_json
```

This lets the orchestrator customize per-subtask without changing the worker's default `SandboxConfig`.

#### 3. `ClaudeCodeAdapter.build_request` — consume new fields

When `SandboxConfig` or `Subtask.agent_config` specifies tools/mcp/etc, append the corresponding CLI flags:

```python
def build_request(self, prompt, working_dir, config, subtask_config=None):
    args = ["-p", prompt, "--output-format", "stream-json", "--max-turns", "20",
            "--dangerously-skip-permissions"]

    # Merge config-level and subtask-level overrides
    cfg = _merge_agent_config(config, subtask_config)

    if cfg.get("tools"):
        args += ["--tools"] + cfg["tools"]
    if cfg.get("allowed_tools"):
        args += ["--allowedTools"] + cfg["allowed_tools"]
    if cfg.get("disallowed_tools"):
        args += ["--disallowedTools"] + cfg["disallowed_tools"]
    if cfg.get("mcp_configs"):
        args += ["--mcp-config"] + cfg["mcp_configs"]
    if cfg.get("append_system_prompt"):
        args += ["--append-system-prompt", cfg["append_system_prompt"]]
    if cfg.get("agent_name"):
        args += ["--agent", cfg["agent_name"]]
    if cfg.get("agents_json"):
        args += ["--agents", cfg["agents_json"]]
    ...
```

#### 4. `Worker._execute_in_sandbox` — pass `subtask.agent_config`

Worker merges subtask-level config into the sandbox call.

#### 5. `Worker.capabilities` — derive from `SandboxConfig`

Instead of hardcoded `["code", "search", "memory", "test"]`, derive capabilities from the actual tools available:

```python
def _derive_capabilities(self) -> list[str]:
    caps = ["code"]
    if self._sandbox_config.mcp_configs:
        caps.append("mcp")
    if self._sandbox_config.tools and "mcp__codegraph__*" in self._sandbox_config.tools:
        caps.append("codegraph")
    # etc.
    return caps
```

ponytail: start with simple string matching — upgrade to tool introspection if needed.

#### 6. Rust side — extend `SandboxConfig` / `ExecRequest` (optional, defer)

The Rust `SandboxConfig` and `ExecRequest` structs need matching fields for the gRPC path. **Defer** until the Python path is validated — the subprocess path is the primary execution mode for distributed workers.

## Scope

### In Scope
- `SandboxConfig` new fields (Python)
- `Subtask.agent_config` field
- `ClaudeCodeAdapter.build_request` flag generation
- `Worker._execute_in_sandbox` config merge
- `Worker.capabilities` derivation from config
- `available_agents()` / `create_adapter()` — no change needed (claude-code already exists)
- Tests for new config fields and CLI arg generation

### Out of Scope
- Rust-side `SandboxConfig`/`ExecRequest` extension (defer to next PR)
- Docker sandbox backend changes (same subprocess path, just containerized)
- MCP server lifecycle management (workers assume MCP configs point to accessible servers)
- Dynamic MCP server discovery / hot-reload
- Codex adapter changes (Codex CLI doesn't support MCP/tools flags)

## Files to Modify

| File | Change |
|------|--------|
| `python/ultimate_coders/agent/sandbox.py` | Add fields to `SandboxConfig`, update `ClaudeCodeAdapter.build_request` |
| `python/ultimate_coders/agent/types.py` | Add `agent_config` to `Subtask`, update `to_dict`/`from_dict` |
| `python/ultimate_coders/agent/worker.py` | Pass `subtask.agent_config` to sandbox, derive capabilities |
| `tests/python/test_sandbox.py` | Test new config fields, CLI arg generation |

## Acceptance Criteria

1. `SandboxConfig` accepts `tools`, `allowed_tools`, `disallowed_tools`, `mcp_configs`, `append_system_prompt`, `agent_name`, `agents_json`
2. `Subtask` carries `agent_config` dict that overrides `SandboxConfig` per-subtask
3. `ClaudeCodeAdapter.build_request` generates correct CLI flags when config fields are set
4. Worker passes subtask-level agent_config to sandbox execution
5. `Worker.capabilities` reflects available tools from config (at minimum "mcp" when mcp_configs is set)
6. Existing tests pass; new test covers CLI arg generation
7. Backward compatible — all new fields default to None/empty, existing behavior unchanged
