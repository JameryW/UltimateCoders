# worker 实时 LSP 后端（multilspy）替代 codegraph worktree 滞后

## Goal

codegraph 是预计算 SQLite 图谱，索引滞后写入 ~1s，对 worker worktree 内编辑中的代码可能未索引——agent 问"find references to X"时答案过期。用实时 LSP 后端（multilspy）替代，让符号操作反映 worktree 当前文件状态。

## Research References

- [`research/realtime-lsp-options.md`](research/realtime-lsp-options.md) — 推荐 Approach A (multilspy)，Python-only MVP 起步，分阶段加语言

## What I already know

- 仓库内无任何实时 LSP server（grep tower-lsp/pylsp/gopls/rust-analyzer 零命中）。
- codegraph = `.codegraph/codegraph.db` 预计算图谱，worker 通过 `mcp__codegraph__*` 工具用（worker.py:313）。
- 既有 in-process MCP 模式：`engine_mcp.py`（search/memory）、`fs_mcp.py`（read/write/edit_file）——`lsp_mcp.py` 照此模板。
- worker Dockerfile = `python:3.11-slim` + git，无 Node/rust-analyzer/gopls。
- **multilspy**：MIT，134KB wheel，Python>=3.8，async API，包 10 语言 LSP server。需 5 方法：`request_definition`/`request_references`/`request_hover`/`request_document_symbols`/`request_workspace_symbol`。pre-alpha v0.0.15，pin 版本。
- multilspy pin `jedi-language-server==0.41.3`（latest 0.47.0），Python-only MVP 只需 `pip install multilspy`（jedi 随依赖来，无需 Node/Go/Rust）。
- multilspy 首次用自动下载 LSP binary 到 `~/.multilspy/lsp/`——Docker build 时预缓存。

## Decision (ADR-lite)

**Context**: codegraph 对 worktree 实时性弱，需实时 LSP。研究比了 4 方案（multilspy / DIY LSP / tree-sitter / codegraph refresh）。
**Decision**: Approach A — multilspy，Python-only MVP。新 `lsp_mcp.py`（uc-lsp MCP server）照 engine_mcp/fs_mcp 模板，wrap multilspy LanguageServer，暴露 5 LSP 工具。worker 注册它，能力标签派生 `lsp`（与 codegraph 共用，不破坏既有）。Python-only 起步，其他语言分阶段加。
**Consequences**: 新增 1 Python dep（multilspy）；Dockerfile 需预缓存 multilspy binary；pre-alpha 风险用 try/except 优雅降级（返回空结果，同 CodegraphClient）缓解；只覆盖 Python（其他语言仍 fallback codegraph 或 read_file+grep）。

## Requirements

- 新 `lsp_mcp.py`（uc-lsp MCP server）暴露：go_to_definition / find_references / hover / document_symbols / workspace_symbol。
- 用 multilspy LanguageServer，workspace = worker worktree 路径（`--workspace` arg，同 fs_mcp）。
- 文件读取实时：每次工具调用前对目标文件发 `didChange`（全量内容，非增量），确保 LSP 看到当前状态。
- worker.py 注册 uc-lsp（同 uc-engine/uc-fs 模式），`_derive_capabilities` 派生 `lsp` 标签。
- 优雅降级：multilspy 未装/启动失败/语言不支持 → 返回空结果 + 提示，不崩溃 MCP server。
- Dockerfile 预缓存 multilspy Python binary（build 时跑一次）。
- Python-only MVP：其他语言不阻塞（工具调用返回"语言不支持，用 codegraph/read_file"）。

## Acceptance Criteria

- [ ] `lsp_mcp.py` 暴露 5 LSP 工具，可被 spawn 的 agent 调用。
- [ ] 对 Python 文件，go_to_definition/find_references/hover 返回实时结果（编辑后立即反映）。
- [ ] worker 注册 uc-lsp，声明 `lsp` 能力标签。
- [ ] multilspy 未装时 MCP server 优雅降级（不崩，返回提示）。
- [ ] 非Python语言调用返回明确"不支持"提示，不崩。
- [ ] 既有 worker/llm/capability 测试不回归。
- [ ] Dockerfile 预缓存 multilspy binary（build 时）。

## Definition of Done

- Tests added（lsp_mcp 工具单测，mock multilspy 或用小 Python fixture）
- Lint / typecheck / CI green
- pyproject.toml 加 multilspy 依赖（optional，不强制装）
- 文档更新（worker 能力 + uc-lsp 用法）

## Technical Approach

1. **`lsp_mcp.py`**（照 fs_mcp 模板）：`_create_server(workspace)` + `list_tools`（5 工具）+ `call_tool`。每个工具：读目标文件 → `ls.request_*` → 返回 TextContent。multilspy LanguageServer lazy 启动（首次调用），keepalive 整个 MCP 生命周期。
2. **优雅降级**：`try: import multilspy except ImportError: Server=None`（同 engine_mcp 的 mcp 包模式）；LanguageServer 启动失败 try/except 返回空+提示。
3. **worker.py**：mcp_configs 追加 `{"uc-lsp": {...}}`（同 uc-fs）；`_derive_capabilities` 的 `_MCP_CAP_ALIASES` 加 `"mcp:uc-lsp": "lsp"`。
4. **依赖**：pyproject.toml 加 `multilspy = {version="==0.0.15", optional=true}`（optional group，不强制装——未装时降级）。
5. **Dockerfile**：build 阶段跑 `python -c "from multilspy.language_server import LanguageServer; ..."` 预缓存 binary 到 `~/.multilspy/lsp/`。
6. **测试**：`test_lsp_mcp.py`——mock multilspy LanguageServer，测 5 工具返回 + 降级路径 + 路径隔离。

## Out of Scope

- 非 Python 语言 LSP（Rust/Go/TS）——分阶段，MVP 只 Python。
- multilspy 增量 didChange（用全量，简单正确）。
- 文件 watch 主动 didChange（按需查时发，不监听）。
- 替换 codegraph（并存，codegraph 仍用于跨仓库/历史索引）。
- fork vendor / Path A 硬禁（另一 A 子项，独立 task）。

## Technical Notes

- 模板：engine_mcp.py、fs_mcp.py。
- multilspy 5 方法：request_definition/request_references/request_hover/request_document_symbols/request_workspace_symbol。
- 关键文件：worker.py:313（codegraph 标签）、worker.py:177（mcp_configs）、_MCP_CAP_ALIASES。
- ponytail：lsp_mcp.py 照模板，不引入 LSP 协议细节，全靠 multilspy 抽象。
