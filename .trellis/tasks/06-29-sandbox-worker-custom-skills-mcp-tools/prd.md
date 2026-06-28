# Sandbox Worker Custom Skills & MCP Tools

## Goal

让 sandbox 模式下的 coding agent（Claude Code / Codex）支持自定义 skill、MCP tool 等能力，使 worker 可以按需配置和暴露不同的工具集，实现能力感知调度。

## Requirements

1. **CodexAdapter 消费 subtask_config** — 根据 subtask_config 写临时 `config.toml`，配置 MCP servers 和 enabled_tools
2. **内联 MCP 配置** — `mcp_configs` 支持文件路径和内联 JSON 对象两种形式，adapter 执行前将内联 JSON 写临时文件再传 `--mcp-config`
3. **Worker agent_config 自动推导** — 按 subtask 类型硬编码模板 + 按 required_capabilities 匹配 profile，capability 匹配优先
4. **Worker._derive_capabilities 增强** — 更细粒度推导能力标签（skill, mcp:xxx, agent:xxx 等）

## Acceptance Criteria

* [ ] CodexAdapter.build_request 正确合并 subtask_config，写临时 config.toml
* [ ] 内联 MCP JSON 写临时文件后传 `--mcp-config`（Claude Code adapter）
* [ ] 内联 MCP JSON 写临时 config.toml（Codex adapter）
* [ ] Worker 按 subtask 类型自动推导 agent_config（模板 + capability 匹配）
* [ ] _derive_capabilities 覆盖 skill/mcp/agent 等维度
* [ ] 端到端：subtask 带 required_capabilities → worker 推导 agent_config → sandbox 执行时正确传 CLI flags
* [ ] 临时文件在执行后清理

## Definition of Done

* Tests added/updated
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes

## Technical Approach

### 1. 内联 MCP 配置处理

`_merge_agent_config` 已有合并逻辑，新增 `_resolve_mcp_configs` 辅助函数：
- 遍历 `mcp_configs` 列表
- 如果元素是文件路径（字符串且文件存在），保持原样
- 如果元素是 dict/JSON 对象，写临时文件，返回路径
- 临时文件注册到 cleanup 列表，执行后删除

### 2. CodexAdapter.build_request 改造

- 合并 subtask_config（复用 `_merge_agent_config`）
- 根据 merged config 写临时 `~/.codex/config.toml`：
  - `mcp_configs` → `[mcp_servers]` section
  - `tools` → `enabled_tools` per server
  - `allowed_tools`/`disallowed_tools` → tool approval 配置
- 执行后清理临时 config

### 3. Worker agent_config 自动推导

新增 `Worker._resolve_agent_config(subtask)` 方法：
- 如果 subtask 已有 agent_config，直接用（用户显式覆盖）
- 否则按 required_capabilities 匹配预定义 profile
- 最后按 subtask 类型应用模板覆盖（review → disallow Edit/Write）

预定义 profiles：
```python
AGENT_PROFILES = {
    "review": {"disallowed_tools": ["Edit", "Write", "NotebookEdit"], "append_system_prompt": "Read-only review mode"},
    "codegraph": {"tools": ["default", "mcp__codegraph__*"], "mcp_configs": [...]},
    "code": {"tools": ["default"]},  # 默认
}
```

Subtask 类型模板：
```python
SUBTASK_TEMPLATES = {
    "review": AGENT_PROFILES["review"],
    "search": {"tools": ["default", "mcp__codegraph__*"]},
}
```

### 4. _derive_capabilities 增强

从 SandboxConfig 推导更细粒度的能力标签：
- `mcp_configs` → `"mcp"` + 每个 MCP server 的具体能力（如 `"mcp:codegraph"`）
- `tools` → 逐个解析 `"mcp__<server>__*"` → `"mcp:<server>"`
- `agent_name` → `"agent:<name>"`
- `agents_json` → 解析出 agent name → `"agent:<name>"`

## Decision (ADR-lite)

**Context**: Codex CLI 工具扩展方式（config.toml）与 Claude Code（CLI flags）完全不同
**Decision**: Codex adapter 通过写临时 config.toml 实现，与 Claude Code adapter 统一在 subtask_config 层面
**Consequences**: 需要临时文件管理（写+清理），Codex 的 MCP 配置格式与 Claude Code 不同需分别处理

**Context**: MCP 配置来源
**Decision**: 支持内联 JSON（方案 B），adapter 执行前写临时文件
**Consequences**: 更灵活，分布式 worker 不需要预装所有 MCP 配置文件

**Context**: OMP 编排侧是否感知工具配置
**Decision**: OMP 不感知（方案 B），agent_config 只在 Python 层设置
**Consequences**: SubtaskDef 不需要改，但 OMP 无法按工具需求调度 worker

**Context**: agent_config 推导策略
**Decision**: 默认模板 + capability 匹配混合（方案 C）
**Consequences**: 两种机制互补，capability 匹配优先于类型模板

## Out of Scope

* SubtaskDef (TypeScript) 增加 agentConfig — OMP 不感知工具细节
* Rust SandboxConfig 新增字段 — 当前 Vec<String> 已够用，内联 JSON 在 Python 层解析
* Skill 自动发现/注册 — Skills 通过现有 `--agents` 和 `--tools` 机制传递

## Technical Notes

* Python SandboxConfig: `python/ultimate_coders/agent/sandbox.py:27`
* Rust SandboxConfig: `crates/uc-engine/src/sandbox/mod.rs:72`
* ClaudeCodeAdapter: `python/ultimate_coders/agent/sandbox.py:728`
* CodexAdapter: `python/ultimate_coders/agent/sandbox.py:912`
* _merge_agent_config: `python/ultimate_coders/agent/sandbox.py:704`
* Worker._derive_capabilities: `python/ultimate_coders/agent/worker.py:199`
* Subtask.agent_config: `python/ultimate_coders/agent/types.py:101`
* Codex config.toml format: `research/codex-cli-capabilities.md`
* Claude Code CLI flags: `research/claude-code-cli-flags.md`
