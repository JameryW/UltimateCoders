# PRD: 跨仓库检索与记忆共享深度集成

## Problem

基础能力已实现（Worker gRPC Engine、搜索上下文注入、记忆共享方法），但存在 5 个集成缺口：

1. **Orchestrator 分解时无搜索上下文** — subtask 创建时不参考跨仓库代码，分解质量受限
2. **OMP TUI 无法触发跨仓库检索** — `/uc submit` 不传 projectId，无 `/uc search` 命令
3. **NATS dispatch 丢失 agent_config_json** — subtask 的 agent 配置（tools, MCP, system prompt）在远程派发时丢失
4. **Sandbox agent 无法动态搜索** — 预注入的搜索结果只是静态文本，agent 执行中无法追加搜索或读写共享记忆
5. **无集成测试** — project_id 从 dispatch → 搜索注入 → sandbox prompt 的完整路径未验证

## Solution

### Phase 1: Quick Fixes（3 项，各 < 10 行改动）

1. **Rust server.rs**: `agent_config_json: None` → `st.agent_config_json.clone()`（2 处）
2. **OMP submitTask**: `projectId` 从 `""` → 接受用户输入参数
3. **Orchestrator.submit_task**: 添加 `_build_search_context` 调用注解 subtask 描述

### Phase 2: Sandbox Agent Tools（中等，需设计）

4. **Engine MCP Server**: 轻量本地 MCP server 包装 `Engine.search()` + `read_memory()` + `write_memory()`，注册到 SandboxConfig 的 mcp_configs

### Phase 3: Integration Test（中等）

5. **Python 集成测试**: mock Engine + NATS + Worker，验证 project_id 传播和搜索上下文注入

## Scope

- **In**: Quick fixes (3)、Engine MCP Server (1)、集成测试 (1)
- **Out**: 不改 HybridSearchEngine 逻辑、不加新存储后端、不改 OMP TUI layout

## Acceptance Criteria

1. `publish_ready_subtasks` 和 `dispatch_ready_subtasks` 传递 `agent_config_json`
2. `submitTask` RPC 接受 `projectId` 参数并传播到 task
3. Orchestrator.submit_task 搜索上下文注解 subtask 描述
4. Sandbox agent 可通过 MCP tools 调用 `search_code` / `read_memory` / `write_memory`
5. 集成测试验证 project_id → 搜索注入 → prompt enrichment 完整路径
6. 现有测试不回归
