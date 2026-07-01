# uc-lsp 自动 fallback codegraph

## Goal

uc-lsp 对不支持的场景（非 Python 语言、multilspy 未装、语言不支持）当前返回"不支持，用 codegraph 或 read_file"提示，让 agent 自己决定。改为 uc-lsp 自动 fallback 到 codegraph（同进程 CodegraphClient）尽力提供等价结果，agent 无感知，体验更顺。

## What I already know

- uc-lsp 5 工具：go_to_definition/find_references/hover/document_symbols/workspace_symbol。
- CodegraphClient（`codegraph.py`）API：`search(query)`/`callers(symbol)`/`callees(symbol)`/`impact(symbol)`/`explore(query)`/`is_available()`。**无 1:1 的 go_to_definition/find_references/hover/document_symbols**。
- codegraph 是跨仓库调用图 + FTS 搜索，非 per-file 符号树；hover 无类型/文档信息。
- fallback 触发条件：multilspy 未装 / 语言非 Python / LanguageServer 启动失败。

## Decision (ADR-lite)

**Context**: codegraph API 与 LSP 5 工具非 1:1，部分工具无等价（hover 类型、document_symbols 文件树）。
**Decision**: 尽力而为 fallback——能映射的工具自动转发 codegraph，不能映射的明确返回"codegraph 无此语义，建议 read_file + grep"。不假装能提供 LSP 的语义精度。
**Consequences**: agent 不用手动切换工具；但 codegraph fallback 结果精度低于真 LSP（无类型/文档/per-file 树），文档需说清。

## Requirements

- uc-lsp 在 multilspy 不可用/语言不支持时，自动尝试 codegraph fallback（同进程 CodegraphClient，workspace 路径）。
- 语义映射：
  - `workspace_symbol(query)` → `codegraph.search(query)`
  - `find_references(path,line,char)` → 先 `codegraph.search(symbol)` 定位符号名，再 `codegraph.callers(symbol)` 并集
  - `go_to_definition(path,line,char)` → `codegraph.search(symbol)` 取首个定义位置
  - `hover` → codegraph 无等价 → 返回"codegraph 无 hover 语义，建议 read_file 看上下文" + 符号所在位置（若有）
  - `document_symbols(path)` → codegraph 无 per-file 树 → 返回"codegraph 无 document_symbols，建议 read_file 或 workspace_symbol 按文件路径过滤"
- codegraph 不可用（is_available()=False）时，回退到当前的"不支持"提示。
- 结果标注来源（"[codegraph fallback]"前缀），让 agent 知道精度可能不同。
- 既有 uc-lsp 测试不回归；新增 fallback 路径测试。

## Acceptance Criteria

- [ ] multilspy 未装时，uc-lsp 的 workspace_symbol/find_references/go_to_definition 自动走 codegraph fallback。
- [ ] hover/document_symbols 在 fallback 时返回明确的"无等价语义"提示 + 替代建议。
- [ ] codegraph 不可用时回退到"不支持"提示，不崩。
- [ ] fallback 结果带"[codegraph fallback]"来源标注。
- [ ] 既有 25 个 lsp_mcp 测试 + 508 全套不回归。
- [ ] 新增 fallback 路径测试覆盖。

## Definition of Done

- Tests added（fallback 路径单测，mock CodegraphClient）
- Lint / CI green
- 文档（lsp_mcp docstring）说明 fallback 行为与精度局限

## Technical Approach

1. **CodegraphClient 集成**：lsp_mcp.py lazy import CodegraphClient，`_get_codegraph(workspace)` lazy 构造 + 缓存（同 `_ls_cache` 模式）。
2. **fallback 调度**：每个工具函数，当 `_resolve_ls` 返回降级原因时，调 `_fallback_<tool>(workspace, args)` 走 codegraph。
3. **符号名提取**：find_references/go_to_definition 需要 path+line+char → 符号名。codegraph 无按位置反查，用 `read_file` 读该行 + 简单 token 提取（regex 抓 identifier）作为符号名 best-effort。
4. **来源标注**：fallback 返回的 TextContent 文本前加 `[codegraph fallback] `。
5. **测试**：mock CodegraphClient，测每个工具的 fallback 路径 + codegraph 不可用回退 + 来源标注。

## Out of Scope

- 加 Rust/TS LSP 支持（独立 task）。
- codegraph 实时刷新（仍是预计算，fallback 接受其滞后性）。
- per-file 符号树（document_symbols 真正实现，需 tree-sitter，独立 task）。
- hover 类型信息（需真 LSP，codegraph 无）。

## Technical Notes

- 关键文件：lsp_mcp.py（加 fallback）、codegraph.py（API：search/callers/callees/explore）。
- ponytail：fallback 用 best-effort 映射，不追求 LSP 精度；符号名提取用简单 regex，不引入 parser。
