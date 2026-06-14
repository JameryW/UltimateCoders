# 为 Coding Agent 集成 Codegraph 能力

## Goal

为 UltimateCoders 的 coding agent（Worker）集成 codegraph 代码知识图谱能力，使其在执行 subtask 时具备结构化代码理解力——包括符号搜索、调用图分析、影响范围评估和受影响测试检测——从而减少盲目的 grep/read 探索，提升修改精度和安全性。

## What I already know

* Codegraph 已在本项目中作为 MCP server 运行（`codegraph serve --mcp`），索引了 133 个文件、3,158 个节点、7,551 条边
* Codegraph 提供 8 个 MCP 工具：`explore`、`node`、`search`、`callers`、`callees`、`impact`、`files`、`status`
* Worker 已有 tool-calling 循环（`_build_tools()` / `_build_tool_definitions()`），注册了 search、memory、read_file、list_files
* Sandbox 模式下 Worker 通过 `SandboxManager.execute()` 调用 `claude -p "<prompt>"`，prompt 中可注入上下文
* Codegraph CLI 支持 `--json` 输出，可直接解析
* `.codegraph/codegraph.db` 是标准 SQLite，可从 Python 直接查询
* 已安装 codegraph v0.9.9（v1.0 已发布，新增 `explore`/`node` CLI 命令）
* 7,662 个未解析引用（Python↔Rust 跨语言边界无法 tree-sitter 解析）

## Assumptions (validated)

* `ANTHROPIC_API_KEY` 和 `codegraph` CLI 在运行环境中可用
* `.codegraph/codegraph.db` 在项目根目录存在且由 daemon 保持同步
* Worker 的 LLM tool-calling 模式和 sandbox 模式都需要 codegraph 支持

## Decision (ADR-lite)

### ADR-1: 集成架构 — 混合方案

**Context**: 需要平衡灵活性与效率。LLM 模式需要运行时工具，sandbox 模式需要在 prompt 中预注入上下文。
**Decision**: 混合方案（C）— 两层集成：
1. **预处理层**：在 subtask 执行前，自动用 codegraph 查询构建结构化上下文，注入到 sandbox prompt 或 LLM 消息中
2. **工具层**：在 LLM Worker 的 tool-calling 循环中新增 codegraph 工具，让 LLM 可以深度探索
**Consequences**: sandbox 模式的 Claude Code 实例从一开始就有方向感；LLM 模式可以按需深入查询；两层共用同一个 `CodegraphClient` 实现

### ADR-2: 查询实现 — 直接 SQLite

**Context**: CLI 子进程每次开销 ~100-200ms；MCP server 需要 stdio 通信；直接 SQLite 访问零开销。
**Decision**: Python `CodegraphClient` 类直接读取 `.codegraph/codegraph.db`，通过 `sqlite3` 模块查询。封装 5 个核心方法：search、callers、callees、impact、explore。
**Consequences**: 查询延迟降至 <1ms；无子进程开销；需处理 SQLite WAL 锁和 schema 版本兼容；无法获得 codegraph 的 `buildContext`/`explore` 智能上下文组装逻辑（需自行实现简化版）

### ADR-3: 预处理上下文格式

**Context**: 注入到 sandbox prompt 的上下文需要结构化但不过长。
**Decision**: 预处理输出 Markdown 格式，包含：
- 相关符号列表（name, kind, file:line, signature）
- 依赖关系（callers/callees，1 层深度）
- 影响范围摘要（impact depth=1 的节点数）
- 受影响测试文件列表
总长度限制 ~2000 字符，超出截断并标注
**Consequences**: Claude Code 在 sandbox 中获得即时方向感，无需额外探索即可理解修改影响范围

## Requirements

* 新增 `python/ultimate_coders/agent/codegraph.py` — `CodegraphClient` 类
  - 直接 SQLite 查询，5 个核心方法：search、callers、callees、impact、explore
  - 自动检测 `.codegraph/codegraph.db`，不存在时优雅降级（返回空结果）
  - FTS5 搜索 + 结构化查询组合
  - explore 方法：组合 search + callers/callees/impact 返回结构化上下文
* 新增 `CodegraphToolProvider` — 在 Worker LLM 模式中提供 codegraph 工具
  - 注册到 `_build_tools()` 和 `_build_tool_definitions()`
  - 5 个工具：symbol_search、find_callers、find_callees、impact_analysis、explore_code
* 改造 `Worker._execute_with_llm()` — 注入 codegraph 预处理上下文
  - 在 `_gather_prior_context()` 中调用 `CodegraphClient.explore(subtask.description)`
  - 结果作为 prior_context 的一部分注入
* 改造 `Worker._execute_in_sandbox()` — 注入 codegraph 预处理上下文
  - 在构建 sandbox prompt 时，将 codegraph 上下文作为前置段落
* 优雅降级：codegraph 不可用时（DB 不存在、查询失败），Worker 正常运行，只是没有 codegraph 增强
* 单元测试：`tests/python/test_codegraph.py`
  - CodegraphClient 核心方法测试（使用临时 SQLite DB）
  - 降级场景测试
  - Worker 集成测试（mock codegraph）

## Acceptance Criteria

* [ ] `CodegraphClient` 可直接查询 `.codegraph/codegraph.db`
* [ ] LLM Worker 的 tool-calling 循环包含 5 个 codegraph 工具
* [ ] Sandbox 模式执行前自动注入 codegraph 上下文到 prompt
* [ ] codegraph DB 不存在时 Worker 正常运行（无 codegraph 增强）
* [ ] 所有新测试通过，无回归
* [ ] ruff lint 通过

## Definition of Done

* 新模块 + 测试提交
* 现有 297 测试不受影响
* Lint / typecheck 通过
* 代码文档更新

## Out of Scope (explicit)

* codegraph CLI 升级到 v1.0（后续可选）
* 跨语言引用解析（Python↔Rust FFI 边界）
* codegraph MCP server 在 sandbox 内的配置（Claude Code 自身已有 MCP 访问）
* Dashboard/TUI 集成（仅 Worker 层）

## Technical Approach

### 核心模块 `codegraph.py`

```python
class CodegraphClient:
    """Direct SQLite client for codegraph knowledge graph."""

    def __init__(self, project_path: str): ...
    def is_available(self) -> bool: ...
    def search(self, query: str, kind: str = None, limit: int = 10) -> list[dict]: ...
    def callers(self, symbol: str, limit: int = 20) -> list[dict]: ...
    def callees(self, symbol: str, limit: int = 20) -> list[dict]: ...
    def impact(self, symbol: str, depth: int = 2) -> list[dict]: ...
    def explore(self, query: str, max_nodes: int = 15) -> str: ...  # → Markdown
```

### 集成点

1. **LLM Worker 工具注册** (`worker.py:576-669`)
   - 在 `_build_tools()` 中新增 5 个方法，调用 `CodegraphClient`
   - 在 `_build_tool_definitions()` 中注册工具定义

2. **LLM Worker 预处理上下文** (`worker.py:690-745`)
   - `_gather_prior_context()` 调用 `codegraph.explore(subtask.description)`
   - 追加到 `prior_context` 字符串

3. **Sandbox Worker 预处理上下文** (`worker.py:334-371`)
   - `_execute_in_sandbox()` 中构建 prompt 前注入 codegraph 上下文

### 实现计划（小 PR）

* **PR1**: `CodegraphClient` 类 + 单元测试
* **PR2**: Worker LLM 工具注册 + 预处理上下文注入 + 集成测试
* **PR3**: Worker Sandbox 预处理上下文注入 + 降级场景测试

## Technical Notes

* 关键文件：
  - `python/ultimate_coders/agent/worker.py` — Worker._build_tools() / _build_tool_definitions() / _gather_prior_context() / _execute_in_sandbox()
  - `python/ultimate_coders/agent/llm.py` — LLMClient.complete_with_tools() / make_tool_definition()
  - `python/ultimate_coders/agent/types.py` — ToolDefinition / ToolCall
  - `.codegraph/codegraph.db` — SQLite 知识图谱数据库
* SQLite WAL 模式允许并发读（Worker 查询不影响 codegraph daemon 写入）
* FTS5 MATCH 查询语法：`column : "phrase"` 或简单关键词
* Node kinds: method, function, import, class, enum_member, file, struct, variable, enum, trait
* Edge kinds: contains, calls, references, imports, instantiates, extends, implements

## Research References

* [research/codegraph-integration.md](research/codegraph-integration.md) — Codegraph 项目分析、MCP 工具清单、4 种集成方案对比、SQLite schema 详情
