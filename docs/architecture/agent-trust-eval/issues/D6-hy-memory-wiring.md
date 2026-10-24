# D6-hy-memory-wiring

Status: **decided** (2026-10-24 — round-2 via「继续」adopting Proposed)
Type: grilling
Blocked by: _(none)_
Blocks: P1 Hy-Memory context-path implementation tickets

## Question

Hy-Memory 如何进入主路径：Worker 自动 `recall` 进上下文，还是维持 MCP 自愿调用？「旧事实不删、标记已被取代」是否为硬不变量？外发与 SDK 版本如何钉扎？

## Context

- `hy_memory.py` 已封装；`worker.py` 未调用；MCP tools 已有（assessment G5）。
- 分层 Memory 已在 Worker 上下文路径——Hy-Memory 是认知抽取层，不是替换。
- 用户 P1 + 风险：外部 SDK 版本变动与数据外发合规。

## Resolution

**默认 MCP-only；可选自动注入；supersede 硬约束。**

1. 默认保持 **MCP 自愿调用**；仅当 `UC_HY_MEMORY_AUTO_CONTEXT=1` 时 Worker 构建上下文才自动 inject（默认关，符合新模块先放开关后）。
2. **supersede-not-delete** 在写入路径强制执行，与是否自动注入无关——旧事实标记已被取代，不删除。
3. 生产镜像钉扎 `hy-memory` 版本；密钥不进 argv。
4. 无合规审查通过前默认关且不外发。
