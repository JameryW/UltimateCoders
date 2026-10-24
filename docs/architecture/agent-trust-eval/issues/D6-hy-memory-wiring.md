# D6-hy-memory-wiring

Status: open (decision ticket — not implementation)
Type: grilling
Blocked by: _(none — frontier)_
Blocks: P1 Hy-Memory context-path implementation tickets

## Question

Hy-Memory 如何进入主路径：Worker 自动 `recall` 进上下文，还是维持 MCP 自愿调用？「旧事实不删、标记已被取代」是否为硬不变量？外发与 SDK 版本如何钉扎？

## Context

- `hy_memory.py` 已封装；`worker.py` 未调用；MCP tools 已有（assessment G5）。
- 分层 Memory 已在 Worker 上下文路径——Hy-Memory 是认知抽取层，不是替换。
- 用户 P1 + 风险：外部 SDK 版本变动与数据外发合规。

## Options

1. **自动注入**：开关打开时 Worker 构建上下文自动 recall；post-run remember；supersede 标记为硬约束。
2. **MCP-only 加强**：不自动注入；文档/提示要求 Agent 调用；supersede 在 remember API 层强制。
3. **双路径**：默认 MCP-only；`UC_HY_MEMORY_AUTO_CONTEXT=1` 才自动注入（默认关）。

## Proposed (from intake — needs human confirm)

➡️ **选项 3**：符合「新模块先放开关后」；supersede-not-delete 在 `CodingMemory` 写入路径强制，与是否自动注入无关。生产镜像钉 `hy-memory` 版本；无合规审查通过前默认关且不外发。

## Resolution

(fill in when decided)
