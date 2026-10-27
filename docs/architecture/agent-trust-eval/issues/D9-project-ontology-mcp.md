# D9-project-ontology-mcp

Status: **decided** (2026-10-27 — via「继续」adopting Proposed)
Type: grilling
Blocked by: _(none — P0–P2 MVP shipped; Hy-Memory MCP patterns exist as a peer)_
Blocks: P3 ontology MCP implementation tickets

## Question

项目本体（EvoOntology 风格）经 MCP 可查时，最小词表是什么、权威源在哪、是否允许 Agent 写回演化？与已有 codegraph / engine MCP / Hy-Memory 如何划界，以免第三套「真相」？

## Context

- Map fog：项目本体经 MCP 可查的最小词表与权威源。
- 仓库尚无 ontology MCP；`docs/agents/domain.md` 已指向 `CONTEXT.md` 为领域权威落点，但根目录尚无该文件。
- Codegraph / Engine MCP 提供符号与记忆工具，不是概念词表；Hy-Memory 是轨迹抽取事实，不是项目本体。

## Resolution

**MVP = 人维护的最小 `CONTEXT.md` + 只读 MCP 查询；默认关；不写回。**

1. Authority：人类维护的根目录 `CONTEXT.md`（或缺省时明确 fail soft / 空词表）；不把 codegraph、Hy-Memory 或 LLM 摘要当作本体权威。
2. MCP：只读 `list` / `lookup`（或等价）暴露最小词表（术语 → 一句话定义 + 可选权威路径）；功能开关默认 **off**（例如 `UC_ONTOLOGY_MCP`）；关时不注册工具或注册后软失败。
3. 写回 / 「Evo」演化、自动从代码推断本体、多仓联邦词表 — **延后**；禁止 Agent 静默改写权威文件。
