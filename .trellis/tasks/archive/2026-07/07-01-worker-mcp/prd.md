# 完善 worker 工具能力标签与原生 MCP 工具

## Goal

Worker 当前能力标签只声明 `[code,search,memory,test,decompose,review,mcp,mcp:uc-engine]`，但缺 `lsp/debug/browser/file-edit`。Python 侧 in-process 工具只有 3 个（`search_code`/`read_memory`/`write_memory`），精确符号操作与文件编辑全靠 spawn 的 Claude Code/Codex 子进程。本任务让 worker 原生具备 LSP 与文件编辑能力，并让能力标签如实反映实际配置的工具，调度器据此正确路由。

## What I already know

* 能力标签硬编码在 `python/ultimate_coders/agent/worker.py:224` `_derive_capabilities()`：`["code","search","memory","test","decompose","review"]`，动态追加 `mcp` / `mcp:<server>`（如 `mcp:uc-engine`）/ 可选 `codegraph` / `agent:<name>`。
* Worker 注册：`Worker.capabilities` → `get_info()` → `NatsWorker._register_with_gateway()` (nats_worker.py:1336) → gRPC `RegisterWorker` → Rust `WorkerRegistry::register` (worker_service.rs:187)。
* Rust 能力匹配 `WorkerRegistry::workers_with_capabilities` (worker_service.rs:132) = 严格 AND-subset：worker 必须持有 ALL required caps，且心跳 < 60s。调度门控在 server.rs:1659 / 2182；Python 侧镜像在 nats_worker.py:1417。
* Python worker 无自有 tool dispatcher，工具全靠 spawn 子进程（ClaudeCodeAdapter/CodexAdapter/DecomposeAdapter in sandbox.py），通过 `SandboxConfig.tools/allowed_tools/disallowed_tools/mcp_configs` 配置。
* 唯一 in-process MCP server = `engine_mcp.py`，暴露 `search_code`/`read_memory`/`write_memory`，server 名 `uc-engine`，自动注册在 worker.py:177。
* `AGENT_PROFILES` (worker.py:271-320) 配置每个能力的工具：review/deploy 禁 Edit/Write；code/test/fix/docs 用 `["default"]`；refactor/codegraph/search 加 `mcp__codegraph__*`。
* LSP/debug/browser/file-edit 在 Python worker 完全缺失；file-edit 工具名仅出现在 Rust claude_code.rs 输出解析。

## Assumptions (temporary)

* uc-lsp MCP 可复用既有 codegraph 的 LSP 后端（codegraph.py 是 SQL 客户端，但仓库已有 LSP MCP server 配置见 packages/uc-orchestrator）。待确认 LSP 后端来源。
* uc-fs 文件编辑 MCP 实现 str_replace/edit_file/write_file，操作 worker workspace 内文件（git worktree 已隔离）。
* 能力标签补全 = 在 `_derive_capabilities()` 里按"是否配置了对应 MCP/工具"动态声明，而非无条件添加。

## Open Questions

* LSP 后端：复用仓库内既有 LSP MCP，还是 Python 侧新实现？(research-first)
* MVP 范围：四方向（标签补全 / uc-lsp / uc-fs / AGENT_PROFILES 对齐）是否全部纳入首版，还是分 PR？

## Requirements (evolving)

* 能力标签如实反映 worker 实际配置的工具（含 lsp/fs/browser/debug）。
* 新增 uc-lsp MCP server，暴露 go-to-def / find-refs / hover（至少）。
* 新增 uc-fs MCP server，暴露文件编辑工具。
* AGENT_PROFILES 工具配置与声明的能力标签一致。

## Acceptance Criteria (evolving)

* [x] `_derive_capabilities()` 对配置了 codegraph 的 worker 声明 `lsp`。
* [x] 配置了 uc-fs 的 worker 声明 `file-edit` / `mcp:uc-fs`。
* [x] uc-fs MCP 3 个工具（read_file/write_file/edit_file）可被调用（单测覆盖 round-trip + 路径逃逸）。
* [x] 能力匹配调度按新标签正确路由（Rust `workers_with_capabilities` 既有 17 测试全过，新标签字符串透传自动受益）。
* [x] AGENT_PROFILES read-only profile（review/deploy）不声明 file-edit（engine=None 时不自动注册 uc-fs）。
* [x] 既有 460 个 Python 测试 + 17 个 Rust worker 测试不回归。

## Implementation Status

完成。文件：
- `python/ultimate_coders/agent/fs_mcp.py` — 新 uc-fs MCP server（read_file/write_file/edit_file，workspace 路径隔离）
- `python/ultimate_coders/agent/worker.py` — 注册 uc-fs + `_derive_capabilities` 补 lsp/file-edit/browser/debug + AGENT_PROFILES `file-edit` profile
- `tests/python/test_worker_capabilities.py` — 19 个新测试（路径安全 5 + 工具 8 + 能力派生 6）

## Definition of Done (team quality bar)

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes (worker 能力文档)
* Rollout/rollback considered（新 MCP server 默认开启 or opt-in）

## Decision (ADR-lite)

**Context**: Worker 缺 lsp/file-edit 能力标签与原生文件编辑工具。仓库已有 codegraph MCP 作为 LSP 后端。
**Decision**: Approach A — 复用 codegraph 映射为 `lsp` 标签；新写 `fs_mcp.py`（uc-fs MCP，str_replace/edit_file/write_file）映射为 `file-edit`/`mcp:uc-fs`；AGENT_PROFILES 对齐；browser/debug 留标签扩展点不实现。
**Consequences**: codegraph 对 worktree 实时性弱（已知 ceiling，注释标注，升级路径=实时 LSP，out of scope）；uc-fs 操作 worker workspace 文件，依赖 git worktree 隔离保证安全。

## Technical Approach

1. **uc-fs MCP server** (`python/ultimate_coders/agent/fs_mcp.py`)：照 `engine_mcp.py` 模板，stdio server 名 `uc-fs`，工具：
   - `write_file`（path/content/create_dirs）
   - `edit_file`（path/old_string/new_string/replace_all）—— 精确 str_replace
   - `read_file`（path/offset/limit）—— 配套只读
   - 操作根 = worker workspace（`UC_WORKSPACE` env 或 cwd），路径校验拒绝逃逸 workspace。
2. **注册**：`worker.py:177` mcp_configs 追加 `{"uc-fs": {...}}`，同 uc-engine 模式。
3. **标签补全** (`_derive_capabilities`)：
   - codegraph 配置 → 同时声明 `lsp`（保留 `codegraph` 标签不破坏既有）
   - uc-fs 配置 → 声明 `file-edit`（由 `mcp:uc-fs` 派生，或显式追加）
   - browser/debug：读 env 标志（`UC_CAP_BROWSER`/`UC_CAP_DEBUG`）opt-in 声明，server 本体不实现
4. **AGENT_PROFILES 对齐**：
   - review/deploy（read-only）→ 不变（已禁 Edit/Write），但确保不声明 file-edit
   - refactor/codegraph profile → 已有 codegraph 工具，自然声明 lsp
   - code/test/fix/docs → 加 `mcp__uc-fs__*`？No——这些已靠子进程 Edit/Write，uc-fs 是给需要**原生**文件操作的场景。保持 code profile 不变，新增 `file-edit` profile 显式启用 uc-fs。
5. **测试**：uc-fs 工具单测（write/edit/read + 路径逃逸拒绝）+ `_derive_capabilities` 标签单测 + Rust `workers_with_capabilities` 新标签不回归。

## Implementation Plan (small PRs)

* PR1: `fs_mcp.py` + worker 注册 + `_derive_capabilities` 标签补全 + AGENT_PROFILES `file-edit` profile + 单测（Python）
* PR2（可选）: Rust 侧 `workers_with_capabilities` 增加 lsp/file-edit 标签的调度路由单测 + 文档更新

## Out of Scope (explicit)

* debug / browser MCP server 本体（仅留标签声明扩展点）
* 实时 LSP server（codegraph 的 worktree 实时性升级）
* 重写 worker 自有 tool dispatcher（仍走 spawn 子进程模型）
* Rust 能力匹配算法升级（保持 AND-subset）

## Technical Notes

* 关键文件：worker.py:224 `_derive_capabilities`、worker.py:177 mcp_configs、engine_mcp.py、sandbox.py:69 AGENT_PROFILES、worker_service.rs:132。
* 约束：PyO3 binding + maturin；CI 跑 Py3.9（asyncio 原语需 lazy 构造，见 memory）。
* 复用模式：uc-engine MCP 的注册与实现是 uc-lsp/uc-fs 的模板。

## Research References

* `.mcp.json` — 仓库 LSP 后端 = `codegraph` MCP（`codegraph serve --mcp`），提供 codegraph_explore/callers/node/documentSymbol/goToDefinition/findReferences/hover。**即仓库的 LSP 能力来源**。
* `worker.py:244` 已对 `mcp__codegraph__*` 做特殊分支，追加 `codegraph` 标签——但未映射为 `lsp` 标签。

## Research Notes

### 关键洞察（派生自仓库）

* **LSP 后端已存在 = codegraph MCP**。不需要新写 uc-lsp server。worker.py:244 已识别 codegraph 工具，只需把 `codegraph` 标签同时声明为 `lsp`（调度器按 `lsp` 路由，实际执行靠 spawn 的 agent 调 codegraph MCP 工具）。
* codegraph 是预计算 SQLite 图谱（索引滞后写入 ~1s），对 git worktree 内编辑中的代码可能未索引——这是已知 ceiling，ponytail 注释标注，升级路径是实时 LSP（out of scope）。
* **uc-fs 是真正需要新写的 Python in-process MCP**，照 engine_mcp.py 模板（~200 行 stdio server），操作 worker workspace 文件。
* **browser/debug 标签**：本任务只声明标签 + 留扩展点，不实现 server 本体（Out of Scope）。

### Feasible approaches

**Approach A: 复用 codegraph 为 lsp + 新写 uc-fs** (Recommended)
* How: `_derive_capabilities()` 把 codegraph 配置映射出 `lsp` 标签；新写 `fs_mcp.py`（str_replace/edit_file/write_file）注册为 `mcp:uc-fs` → 声明 `file-edit`；AGENT_PROFILES 对齐。
* Pros: 最小代码，复用既有 LSP 后端，uc-fs 模板成熟。
* Cons: codegraph 对 worktree 实时性弱（已知 ceiling）。

**Approach B: 全部新写 uc-lsp + uc-fs**
* How: Python 侧新写 LSP MCP server（桥接 system LSP 或自实现）+ uc-fs。
* Pros: worktree 实时性好。
* Cons: LSP 后端实现量大，重复造轮子（codegraph 已覆盖）。

**Approach C: 仅补标签，不写任何 server**
* How: 只在 `_derive_capabilities()` 加 lsp/file-edit 标签声明（基于 codegraph + 子进程的 Edit/Write），不新增 in-process MCP。
* Pros: 改动最小。
* Cons: file-edit 仍靠子进程，"原生"能力未真正落地；与用户"完善 worker 工具能力"诉求不完全匹配。
