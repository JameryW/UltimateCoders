# Sandbox 模式 Claude Code 本地部署

## Goal

在本地部署 UltimateCoders 系统，使用 sandbox 模式（Claude Code 作为统一执行引擎），Orchestrator 分解 + Worker 执行均通过 `claude` CLI，无需 Python 层 LLMClient。

## What I already know

* 系统已有完整的 sandbox 基础设施：`SandboxManager` + `ClaudeCodeAdapter` + `SubprocessSandbox`/`DockerSandbox`
* `Worker` 已支持 `execution_mode="sandbox"`（默认值），通过 `_execute_in_sandbox()` 调用 `SandboxManager.execute()`
* `ClaudeCodeAdapter` 构建命令：`claude -p "<prompt>" --output-format json --max-turns 20 --dangerously-skip-permissions`
* 当前 `scripts/run_dashboard.py` 使用 `execution_mode="llm"`，需要改造为 sandbox 模式
* 本地已安装 `claude` CLI v2.1.177
* 已有 `docker-compose.yml` 编排 TiKV/Qdrant/PostgreSQL/NATS
* `Orchestrator.decompose_task()` 当前依赖 `LLMClient.complete()`，需改为通过 Claude Code 执行
* `_parse_decomposition()` 解析 LLMResponse → 需适配 Claude Code JSON 输出格式

## Assumptions (validated)

* `ANTHROPIC_API_KEY` 已在 `.env` 中配置
* 本地部署 = 在开发机上直接运行，默认不走 Docker 容器编排
* sandbox 模式默认不需要 TiKV/Qdrant/PostgreSQL/NATS（in-memory fallback）

## Decision (ADR-lite)

### ADR-1: Sandbox 后端

**Context**: Sandbox 后端选择决定部署复杂度和隔离级别
**Decision**: 两者都支持，默认 subprocess。`SandboxConfig.backend` 字段控制切换：`"subprocess"`（默认，开发）→ `"docker"`（生产）
**Consequences**: MVP 用 subprocess 零依赖启动；Docker 后端需预构建镜像但提供强隔离；后续无需重构即可升级

### ADR-2: Orchestrator 分解

**Context**: Orchestrator task 分解目前依赖 Python LLMClient
**Decision**: Orchestrator 分解也通过 Claude Code 执行（`claude -p "decompose..."`），统一架构，不再依赖 Python 层 LLMClient
**Consequences**: 整个系统只需 `claude` CLI + `ANTHROPIC_API_KEY`；需新增 `DecomposeAdapter`；分解输出格式需严格 JSON 约束

### ADR-3: Dashboard

**Context**: 是否需要 Web UI
**Decision**: 两者都提供 — CLI 快速入口 + Dashboard 可选启动
**Consequences**: CLI 入口更轻量适合调试；Dashboard 适合演示和监控

### ADR-4: 部署范围

**Context**: 是否需要存储基础设施
**Decision**: 渐进式 — 默认纯本地（in-memory fallback），`--with-infra` 可选启动 Docker Compose 存储层
**Consequences**: MVP 零外部依赖启动；生产环境可通过标志升级

## Requirements

* 创建 `scripts/run_sandbox.py` — sandbox 模式本地部署入口脚本
* Worker 使用 Claude Code CLI 执行 subtask
* Orchestrator 分解也通过 Claude Code 执行，不再依赖 Python LLMClient
* 新增 `DecomposeAdapter`：构建分解 prompt → 调用 `claude -p` → 解析 JSON 输出为 Subtask 列表
* 支持 subprocess 后端（默认），可选 Docker 后端（`--backend docker`）
* 正确传递 API key 和环境变量
* CLI 入口：`python scripts/run_sandbox.py "Fix the bug"` 命令行提交 + 打印结果
* Dashboard 可选：`python scripts/run_sandbox.py --dashboard` 启动 Web UI
* 可选 `--with-infra` 启动 Docker Compose 存储层
* TUI 模式：`python scripts/run_sandbox.py --tui` 启动 Textual 终端界面
  - 左侧 SubtaskTree 实时进度
  - 右侧 OutputLog 滚动日志
  - 底部输入框提交新任务
  - 状态栏显示 worker/backend/进度

## Acceptance Criteria

* [ ] `python scripts/run_sandbox.py "Fix the bug in main.rs"` 成功提交 task
* [ ] Orchestrator 通过 `claude -p "decompose..."` 分解 task 为 subtask
* [ ] Worker 通过 `claude -p "<subtask>"` 执行 subtask
* [ ] Claude Code JSON 输出被正确解析为 Subtask 列表 / AgentOutput
* [ ] CLI 模式：命令行打印分解 + 执行结果
* [ ] Dashboard 模式：`--dashboard` 启动 Web UI 显示进度
* [ ] TUI 模式：`--tui` 启动终端界面（SubtaskTree + OutputLog + 输入框 + 状态栏）
* [ ] `--backend subprocess`（默认）和 `--backend docker` 可切换
* [ ] `--with-infra` 可选启动 Docker Compose

## Definition of Done

* 部署脚本可一键启动
* 端到端测试通过（提交 task → Claude Code 分解 → Claude Code 执行 → 结果回传）
* 现有测试不受影响（sandbox 模式是增量，不修改 LLM 模式）
* Lint / typecheck 通过
* 文档更新

## Out of Scope (explicit)

* 远程 gRPC 部署
* TiKV/Qdrant/PostgreSQL 深度集成（`--with-infra` 仅启动容器，不连接）
* Rust 层 sandbox 改造（保持 PyO3 FFI 可选）

## Technical Approach

### 核心改造

1. **新增 `DecomposeAdapter`**（`sandbox.py`）— 复用 `ClaudeCodeAdapter` 的 subprocess 执行逻辑，但 prompt 和输出解析不同：
   - 构建：`claude -p "Decompose this task into subtasks: <task>" --output-format json --max-turns 1`
   - 解析：提取 JSON 数组 `[{description, depends_on, file_constraints, expected_output}]`
   - 返回：`list[Subtask]`

2. **改造 `Orchestrator.decompose_task()`** — 当 `llm_client is None` 但有 `sandbox_manager` 时，走 `DecomposeAdapter` 路径

3. **新建 `scripts/run_sandbox.py`** — 参考现有 `run_dashboard.py` 结构：
   - `SandboxConfig(agent="claude-code", backend=args.backend, project_path=cwd, ...)`
   - `Orchestrator(engine=None, llm_client=None, sandbox_manager=...)`
   - `Worker(execution_mode="sandbox", sandbox_config=...)`
   - `--dashboard` 标志启动 Dashboard
   - `--with-infra` 标志启动 docker-compose

### 实现计划（小 PR）

* **PR1**: `DecomposeAdapter` + `Orchestrator.decompose_task()` sandbox 路径 + 单元测试
* **PR2**: `scripts/run_sandbox.py` CLI + Dashboard 集成 + `--with-infra`
* **PR3**: 端到端测试 + 文档更新

## Technical Notes

* 关键文件：
  - `python/ultimate_coders/agent/sandbox.py` — SandboxManager + ClaudeCodeAdapter（新增 DecomposeAdapter）
  - `python/ultimate_coders/agent/orchestrator.py` — decompose_task()（新增 sandbox 路径）
  - `python/ultimate_coders/agent/worker.py` — Worker._execute_in_sandbox()（已就绪）
  - `scripts/run_dashboard.py` — 参考模板
  - `scripts/run_sandbox.py` — 新建入口脚本
* Claude CLI 已安装：v2.1.177 at `/Users/jameryw/.local/bin/claude`
* Worker 默认 `execution_mode="sandbox"`，但当前脚本用 `"llm"` 覆盖了
