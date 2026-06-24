# PRD: 基于 oh-my-pi 构建 UltimateCoders Orchestrator

## 1. 背景与动机

### 现状问题

当前 UC Orchestrator 是一个 Python 实现的协调器，存在以下核心缺陷：

1. **无真正的 Agent 循环**：`_agent_loop` 是手写的 turn 循环，缺乏流式输出、中途干预、上下文压缩等生产级能力
2. **编辑格式脆弱**：依赖 sandbox (claude -p) 做子任务分解，str_replace 编辑易出错
3. **无 IDE 集成**：没有 LSP/DAP，agent 盲写代码，无法做 rename/find-references
4. **单进程瓶颈**：Worker 执行靠 subprocess，无法利用持久化执行内核
5. **无 inter-agent 通信**：子任务之间只能通过文件系统隐式共享，没有 IRC 式实时通信
6. **上下文管理原始**：token 溢出时粗暴截断，没有 compaction 或 context promotion

### oh-my-pi 的平台价值

oh-my-pi (omp) 提供了一套**可直接编程的 Agent 运行时**：

- **SDK 嵌入**：`createAgentSession()` 从任何 Node/Bun 进程驱动完整 agent
- **RPC 模式**：JSONL over stdio，任何语言可驱动
- **扩展系统**：ExtensionAPI 提供 tools + commands + events + rendering 一体化扩展
- **Swarm 参考**：`@oh-my-pi/swarm-extension` 是一个完整的 DAG 多 agent 编排扩展
- **32 个内置工具**：bash、edit (hashline)、read、search、LSP、DAP、python/js 内核
- **Subagent 原语**：`runSubprocess` + agent 定义 + 结构化输出 + yield

**核心决策：直接基于 omp 开发，而非"借鉴思路改造 UC"。**

## 2. 产品定义

### 目标

将 UC 的 Orchestrator-Worker 协同系统构建为 **omp 扩展**，利用 omp 的 agent 运行时、工具链、子 agent 机制作为基础设施，UC 层只负责：

1. **任务调度策略**：哪个子任务分给谁、何时执行
2. **Memory 集成**：UC 分层 Memory (TiKV + Qdrant + PostgreSQL) 作为 omp 的持久化后端
3. **gRPC 桥接**：保持 Rust 核心引擎与 Python/omp 层的通信
4. **监控面板**：Dashboard 接收 omp agent 的生命周期事件

### 非目标

- 不重写 omp 的 agent loop、edit format、LSP 集成
- 不替代 omp 的 subagent 机制（task tool）
- 不替代 omp 的上下文管理（compaction / snapcompact）

## 3. 架构设计

```
┌─────────────────────────────────────────────────┐
│                  UC Dashboard                     │
│            (Next.js — 现有)                       │
└───────────────┬─────────────────────────────────┘
                │ WebSocket / SSE
                ▼
┌─────────────────────────────────────────────────┐
│             UC gRPC Server (Rust)                │
│         (现有 — 任务提交、状态查询)                │
└───────────────┬─────────────────────────────────┘
                │ gRPC
                ▼
┌─────────────────────────────────────────────────┐
│          UC Orchestrator Extension                │
│        (omp extension — 本 PRD 核心)              │
│                                                   │
│  ┌───────────┐  ┌───────────┐  ┌──────────────┐ │
│  │ /uc-submit │  │ /uc-status│  │ /uc-dashboard│ │
│  │  command   │  │  command  │  │   command    │ │
│  └─────┬─────┘  └─────┬─────┘  └──────┬───────┘ │
│        │              │                │          │
│  ┌─────▼──────────────▼────────────────▼───────┐ │
│  │         UCOrchestrator (TypeScript)          │ │
│  │  - Task decomposition (via omp plan agent)  │ │
│  │  - DAG scheduling (inspired by swarm)       │ │
│  │  - Worker registration & load balancing     │ │
│  │  - Memory bridge (UC TiKV/Qdrant)           │ │
│  │  - gRPC bridge to Rust engine               │ │
│  └──────────────────┬──────────────────────────┘ │
│                     │                             │
│         ┌───────────┼───────────┐                │
│         ▼           ▼           ▼                │
│  ┌────────────┐ ┌────────┐ ┌──────────┐         │
│  │ omp agents │ │ omp    │ │ omp      │         │
│  │ (explore,  │ │ tools  │ │ subagent │         │
│  │  plan,     │ │ (bash, │ │ spawning │         │
│  │  reviewer) │ │  edit, │ │ (runSub- │         │
│  │            │ │  LSP)  │ │ process) │         │
│  └────────────┘ └────────┘ └──────────┘         │
└─────────────────────────────────────────────────┘
```

## 4. 核心模块设计

### 4.1 UC Orchestrator Extension (`packages/uc-orchestrator/`)

**包结构**：
```
packages/uc-orchestrator/
├── package.json          # omp.extensions 入口
├── tsconfig.json
├── src/
│   ├── extension.ts      # ExtensionAPI 工厂 — 注册 commands, tools, events
│   ├── orchestrator.ts   # UCOrchestrator 核心 — 任务调度逻辑
│   ├── scheduler.ts      # DAG 调度器 — 依赖解析、拓扑排序、并发控制
│   ├── memory-bridge.ts  # UC Memory → omp context 桥接
│   ├── grpc-bridge.ts    # gRPC 客户端 — 连接 Rust 核心引擎
│   ├── agents/           # 自定义 agent 定义
│   │   ├── decomposer.md # 任务分解 agent (frontmatter 定义)
│   │   └── supervisor.md # 监督 agent (审查子任务结果)
│   └── tools/            # 自定义 tools
│       ├── uc-memory.ts  # 读写 UC 分层 Memory
│       ├── uc-search.ts  # UC 混合检索 (text + semantic + AST)
│       └── uc-grpc.ts    # gRPC 操作 (任务提交/查询)
└── README.md
```

### 4.2 Extension 入口 (`extension.ts`)

```typescript
export default function ucOrchestratorExtension(pi: ExtensionAPI): void {
  pi.setLabel("UC Orchestrator");

  // 注册 slash commands
  pi.registerCommand("uc-submit", {
    description: "Submit a task to UC orchestrator",
    handler: async (args, ctx) => { /* ... */ },
  });

  pi.registerCommand("uc-status", {
    description: "Check task status",
    handler: async (args, ctx) => { /* ... */ },
  });

  // 注册 LLM 可调用 tools
  pi.registerTool({
    name: "uc_memory",
    description: "Read/write UC layered memory",
    parameters: pi.zod.object({
      action: pi.zod.enum(["read", "write", "search"]),
      scope: pi.zod.string(),
      key: pi.zod.string(),
      content: pi.zod.string().optional(),
    }),
    async execute(id, params, onUpdate, ctx, signal) {
      // 桥接到 UC Memory (TiKV/Qdrant)
    },
  });

  // 生命周期事件
  pi.on("session_start", (ctx) => { /* 连接 gRPC */ });
  pi.on("session_shutdown", (ctx) => { /* 断开 gRPC */ });
}
```

### 4.3 任务提交流程

```
用户 /uc-submit "重构认证模块"
  │
  ▼
Extension command handler
  │
  ├─ 1. 创建 Task 对象 (状态: PLANNING)
  ├─ 2. 通过 gRPC 同步到 Rust TaskStore
  ├─ 3. 用 omp plan agent 做任务规划:
  │     runSubprocess({
  │       agent: "decomposer",
  │       task: description,
  │       output: subtaskSchema,  // 结构化输出
  │     })
  │     → 得到 Subtask[] + 依赖关系
  │
  ├─ 4. DAG 调度器解析依赖 → 拓扑排序 → 分波
  ├─ 5. 按波次 spawn 子 agent:
  │     for each wave:
  │       Promise.all(agents.map(a =>
  │         runSubprocess({ agent: "worker", task: a.description })
  │       ))
  │
  ├─ 6. 每个子 agent 完成后:
  │     - yield 结构化结果
  │     - supervisor agent 审查
  │     - 通过 gRPC 更新 TaskStore
  │     - 触发下一波就绪的子 agent
  │
  └─ 7. 全部完成 → pi.sendMessage() 注入汇总到对话
```

### 4.4 Agent 定义

**decomposer.md** — 任务分解 agent:
```markdown
---
name: decomposer
description: Decompose a task into ordered subtasks with dependencies
tools: read,search,find,lsp,codegraph_explore,uc_search,uc_memory
spawns: ""
output:
  type: object
  properties:
    subtasks:
      type: array
      items:
        type: object
        properties:
          description: { type: string }
          depends_on: { type: array, items: { type: string } }
          files: { type: array, items: { type: string } }
---

You are a task decomposition specialist. Given a high-level task:

1. Use search/read/LSP tools to understand the codebase
2. Break the task into minimal, independently verifiable subtasks
3. Define dependency order (which subtasks must complete before others)
4. Identify critical files for each subtask

Output a structured list of subtasks. Each subtask should be completable
by a single coding agent in one session.
```

**supervisor.md** — 结果审查 agent:
```markdown
---
name: supervisor
description: Review subtask results for correctness and completeness
tools: read,search,find,lsp,ast_grep
spawns: ""
output:
  type: object
  properties:
    approved: { type: boolean }
    issues: { type: array, items: { type: string } }
    suggestions: { type: array, items: { type: string } }
---

You are a code review specialist. Given a subtask and its result:
1. Verify the changes accomplish the stated goal
2. Check for bugs, style issues, missing error handling
3. Confirm tests (if any) pass logically
4. Output structured approval result
```

### 4.5 Memory Bridge (`memory-bridge.ts`)

将 UC 的分层 Memory 暴露为 omp 可用的 tool：

| UC Memory 层 | omp tool 参数 | 后端 |
|---|---|---|
| 短期 Memory | `scope="short_term"` | TiKV (通过 gRPC) |
| 长期 Memory | `scope="long_term"` | Qdrant (语义检索) |
| 结构化元数据 | `scope="metadata"` | PostgreSQL (通过 gRPC) |

omp agent 通过 `uc_memory` tool 读写，bridge 负责路由到正确的后端并做格式转换。

### 4.6 gRPC Bridge (`grpc-bridge.ts`)

保持与 Rust 核心引擎的通信：

- **TaskStore 同步**：任务创建/状态变更通过 gRPC 双向同步
- **Memory 操作**：读写请求路由到 gRPC
- **事件流**：Rust 侧的任务事件推送到 omp extension 的 event handler

### 4.7 DAG 调度器 (`scheduler.ts`)

借鉴 omp swarm-extension 的 DAG 引擎：

```typescript
interface SchedulePlan {
  waves: Subtask[][];  // 拓扑排序后的执行波次
  // 同一波内的 subtask 可并行执行
}

class DAGScheduler {
  // 依赖解析 → 拓扑排序 → 分波
  schedule(subtasks: Subtask[]): SchedulePlan;
  // 子任务完成后，检查是否有新的就绪任务
  onSubtaskComplete(id: string): Subtask[];
  // 检测循环依赖
  detectCycles(subtasks: Subtask[]): string[] | null;
}
```

## 5. 与现有系统的集成

### 5.1 Python Agent 层过渡

| 现有 Python 组件 | omp 替代 | 过渡策略 |
|---|---|---|
| `Orchestrator._agent_loop` | omp Agent runtime | 直接用 omp SDK |
| `LLMClient` | omp `@oh-my-pi/pi-ai` | omp 多 provider 支持 |
| `SandboxManager` | omp `runSubprocess` | omp 子 agent 更强大 |
| `Worker` 执行 | omp worker agent 定义 | 定义专用 agent |
| `plan_task` | omp plan agent + 结构化输出 | omp 内置 agent |
| `ask` | omp 对话 + uc_memory tool | 工具桥接 |

### 5.2 Rust 核心引擎保留

- `uc-types`、`uc-engine`、`uc-grpc`、`uc-grpc-server` 保持不变
- gRPC server 继续服务 Dashboard 和外部客户端
- Python `uc-python` PyO3 层逐步降级为 Memory 桥接薄层
- 新的编排逻辑全部在 omp extension (TypeScript) 中

### 5.3 Dashboard 集成

Dashboard 通过 gRPC/WireProtocol 接收事件，不需要感知 omp 的存在：
- 任务生命周期事件由 extension 通过 gRPC 同步到 Rust TaskStore
- Dashboard 从 TaskStore 读取，不变

## 6. 实施计划

### Phase 1: 脚手架 + 单任务端到端 (1 周)

1. **Fork omp monorepo**，在 `packages/` 下创建 `uc-orchestrator/`
2. **extension.ts**：注册 `/uc-submit` 和 `/uc-status` 命令
3. **decomposer agent**：用 omp plan agent 做任务分解
4. **单任务流程**：submit → decompose → spawn 单 worker → collect result
5. **验证**：`omp` TUI 内 `/uc-submit "fix typo in README"` 跑通

### Phase 2: DAG 调度 + 并行执行 (1 周)

1. **DAGScheduler**：拓扑排序、分波、循环检测
2. **并行 spawn**：波内 `Promise.all(runSubprocess(...))`
3. **supervisor agent**：子任务结果审查
4. **验证**：3+ 子任务带依赖关系的任务跑通

### Phase 3: Memory Bridge + gRPC 桥接 (1 周)

1. **uc_memory tool**：读写 UC 分层 Memory
2. **uc_search tool**：UC 混合检索
3. **gRPC bridge**：连接 Rust TaskStore，同步状态
4. **验证**：任务状态在 Dashboard 实时可见

### Phase 4: 生产化 (1 周)

1. **错误恢复**：子任务失败重试、checkpoint/resume
2. **并发控制**：信号量限制并行子 agent 数量
3. **上下文管理**：利用 omp compaction + snapcompact
4. **Advisor 集成**：omp advisor 做质量把关
5. **IRC 通信**：子 agent 间实时消息
6. **文档 + 测试**

## 7. 关键技术决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 开发语言 | TypeScript (omp extension) | omp 原生扩展语言 |
| Agent 运行时 | omp `createAgentSession` / `runSubprocess` | 不重新发明 |
| 任务分解 | omp plan agent + 结构化输出 | 比 claude -p sandbox 更可控 |
| 子 agent 执行 | omp `runSubprocess` + 自定义 agent 定义 | 内置隔离、重试、yield |
| DAG 调度 | 自建 (参考 swarm-extension) | swarm 的 YAML DAG 太静态，需要动态调度 |
| Memory | UC Rust 引擎 (通过 gRPC) | 已有基础设施，不做重复 |
| 状态同步 | gRPC 双向 | 保持 Dashboard 兼容 |
| 编辑格式 | omp hashline | 比 str_replace 更可靠 |
| LSP | omp 内置 | 不自建 |

## 8. 成功指标

1. **端到端延迟**：单子任务 < 30s（从 submit 到 result）
2. **并行效率**：3 个独立子任务并行执行，总时间 < 1.5x 单任务时间
3. **可靠性**：子任务失败自动重试，3 次重试内恢复率 > 90%
4. **Dashboard 可见**：所有任务状态实时同步到现有 Dashboard
5. **Memory 集成**：agent 可读写 UC Memory，跨任务知识累积

## 8.1 Phase 3-4 Acceptance Criteria

### Phase 3 残余

* [x] `memory-bridge.ts` 独立文件抽取 — uc_memory/uc_search 从 extension.ts 内联代码重构为独立模块
* [ ] Memory bridge 支持所有 3 层 scope (short_term/long_term/metadata)，正确路由到 gRPC

### Phase 4: 错误恢复 + checkpoint/resume

* [x] 子任务失败自动重试 (maxRetries=2, exponential backoff 5s→10s→20s)
* [x] `_save_checkpoint` 保存 modified_files/tool_calls/error/retryCount 到 TaskStore
* [x] `_load_checkpoint` 恢复完整 SubtaskResult (含 retryCount, loadRecoverable 含 failed)
* [x] `/uc resume <task-id>` 从 checkpoint 断点继续 (支持 paused 和 failed 状态)
* [x] 重试次数超限 → 标记子任务 failed + syncTaskToGrpc + writeMemory 通知 Dashboard

### Phase 4: 并发控制信号量

* [x] 信号量限制并行子 agent 数量（默认 maxConcurrency=3, worker pool 模式）
* [ ] 读写子任务动态容量：无 file_constraints → 2x 并发，多 file_constraints → 限制
* [ ] 大量并行任务不 OOM（signal + queue 架构）

### Phase 4: IRC 子 agent 间通信

* [ ] 子 agent 间共享上下文通过 uc_memory tool（替代 IRC）
* [x] 完成的子 agent 结果自动写入 uc_memory（scope: task, key: subtask_result_<id>）
* [x] 后续子 agent prompt 自动注入已完成依赖子任务摘要（500字截断, buildContextForSubtask）
* [ ] 子 agent 可通过 uc_memory tool 主动查询完整细节

### Phase 4: Advisor 集成

* [x] Supervisor agent 做子任务结果审查（approved/issues/suggestions, reviewSubtask）
* [x] 审查超时 (60s) → 自动批准（避免阻塞, reviewTimeoutMs + catch auto-approve）
* [x] 审查拒绝 → 子任务标记 failed + 可重试 (Review rejected → executeSubtaskWithRetry)
* [ ] 审查结果写入 uc_memory + Dashboard 可见

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| omp 版本更新破坏扩展 API | 扩展编译失败 | pin omp 版本，渐进升级 |
| gRPC bridge 延迟 | Dashboard 状态不同步 | 异步批量同步 + WebSocket 直推 |
| omp 子 agent 资源消耗 | 大量并行任务 OOM | 信号量 + 队列，限制并发 |
| Python → TypeScript 迁移成本 | 双系统维护 | Phase 1-3 并行运行，Phase 4 切换 |

## 10. 当前实现状态 (Gap Analysis)

### 已实现

| 模块 | 文件 | 行数 | 状态 |
|------|------|------|------|
| Extension 入口 | `extension.ts` | 229 | ✅ `/uc` command + `uc_memory`/`uc_search` tools |
| Orchestrator 核心 | `orchestrator.ts` | 940 | ✅ submit → decompose → DAG → wave execution → supervisor review → summary |
| DAG Scheduler | `scheduler.ts` | 142 | ✅ 拓扑排序、分波、循环检测 |
| gRPC Bridge | `grpc-bridge.ts` | 320 | ✅ SubmitTask/GetTask/WatchTask + Memory R/W + upsertTask |
| Task 持久化 | `task-store.ts` | 93 | ✅ JSON 文件 save/load/loadRecoverable |
| Agent 定义 | `agents/*.md` | — | ✅ decomposer + supervisor + worker |
| Scheduler 测试 | `scheduler.test.ts` | — | ✅ DAG + cycle detection + TaskStore |
| omp vendor | `vendor/oh-my-pi` | — | ✅ v16.1.16 submodule |

### 未实现 (PRD Phase 3-4)

| 模块 | PRD 引用 | 优先级 |
|------|----------|--------|
| `memory-bridge.ts` | §4.5 | P2 — uc_memory tool 已在 extension.ts 内联，独立 bridge 未抽 |
| `tools/uc-search.ts` | §4.1 tools/ | P3 — 已在 extension.ts 内联 |
| `tools/uc-grpc.ts` | §4.1 tools/ | P3 — 已在 grpc-bridge.ts |
| IRC 子 agent 间通信 | §6 Phase 4 | P4 |
| omp advisor 集成 | §6 Phase 4 | P4 |
| 错误恢复 checkpoint/resume | §6 Phase 4 | P2 — 已有 Python 侧实现，omp 侧缺 |
| 并发控制信号量 | §6 Phase 4 | P3 — maxConcurrency 已有 |

### 关键发现

1. **Phase 1-2 已完成**：extension + orchestrator + scheduler + agents + task-store 全部实现
2. **Phase 3 部分完成**：gRPC bridge ✅、Memory tools ✅（内联）、独立 memory-bridge.ts 缺
3. **Phase 4 未开始**：checkpoint/resume、IRC、advisor、并发控制信号量
4. **uc_memory/uc_search tools 在 extension.ts 内联**，PRD 要求独立文件但功能已存在
5. **Python 侧 Orchestrator 仍在运行**，尚未迁移到 omp 侧

## 11. 双系统对齐策略

### 现状

两套 Orchestrator 并行运行：
- **Python 侧** (NatsWorker + Worker): 完整的重试、checkpoint、心跳、Event Sourcing、并发控制
- **omp 侧** (UCOrchestrator): Phase 1-2 完成，缺 Phase 3-4

### 对齐原则

1. **功能对齐，不是代码对齐** — 两边实现相同的功能契约，代码结构可不同
2. **omp 侧为长期方向** — Python 侧逐步降级为薄桥接层
3. **共享 gRPC 接口** — 两边通过同一个 Rust TaskStore 交互，Dashboard 不需要知道哪个 Orchestrator 在跑

### 功能对齐表

| 功能 | Python 侧 | omp 侧 (本任务) | 对齐标准 |
|------|-----------|----------------|----------|
| 失败重试 | max_retries + backoff | 待实现 | maxRetries=2, exponential backoff |
| Checkpoint | save/load modified_files/tool_calls/error | 待实现 | 同 schema |
| Resume | load checkpoint skip completed | TaskStore resumeFromWave | 对齐 checkpoint 字段 + resume 逻辑 |
| 并发控制 | _dynamic_capacity (read 2x, write limit) | 待实现 | 同策略 |
| 子 agent 间通信 | uc_memory tool + context injection | buildContextForSubtask (500字) | 对齐 |
| 心跳超时 | _stale_worker_cleanup_loop (90s) | N/A (omp 管理子进程) | 不对齐 |
| Event Sourcing | JetStream consumer + replay | N/A (omp 无 NATS) | 不对齐 |

### 共存模式

- **Python 模式**: `python -m ultimate_coders.nats_worker` — 通过 NATS
- **omp 模式**: `omp` TUI 内 `/uc submit` — 通过 gRPC
- **互斥**: 同一 task 不应同时被两边处理
- **Dashboard 统一**: 两边都通过 gRPC 同步到同一个 TaskStore
