# Distributed AI Coding System (UltimateCoders)

## Goal

设计并实现一个分布式 AI Coding 系统（UltimateCoders），支持多个 coding agent 以 Orchestrator-Worker 模式协同工作，共享分层 Memory，并集成多 Git 仓库的混合检索能力（Text + Semantic + AST）。让多个 AI Agent 像开发团队一样协作完成编码任务。

## Requirements

### 核心功能

1. **多 Coding Agent 编排（Orchestrator-Worker）**
   - Orchestrator 接收用户任务，LLM 驱动分解为子任务 + 依赖 DAG
   - Worker 注册能力，Orchestrator 推送分配子任务（NATS `task.assign.{worker_id}`）
   - 子任务结果按拓扑序聚合，Orchestrator 适时调整后续计划
   - Worker 心跳通过 NATS 广播，Orchestrator 监控存活状态

2. **分层 Memory 系统（分布式存储）**
   - 短期层（TiKV）：任务级上下文（代码变更 diff、决策记录、进度状态），易变、快速 KV 访问
   - 长期层（Qdrant）：项目级知识库（架构理解 embedding、决策历史、模式积累），持久、语义可检索
   - 统一 EngineApi trait 接口，读写操作跨层透明
   - Memory 事件通过 NATS 广播，支持跨 Agent 实时感知

3. **多 Git 仓库混合检索（Text + Semantic + AST）**
   - 文本级：语言感知分词器（camelCase/snake_case 拆分）+ trigram/ngram 索引
   - 语义级：AST-aware 分块 → Voyage Code 3 embedding → Qdrant 向量检索
   - AST 级：tree-sitter 解析 → PostgreSQL symbols/references 表 → 结构化查询
   - 索引管线：增量（git diff）为主，全量（新仓库/schema 迁移）为备
   - 一致性保障：webhook（主）+ polling（备）+ 定期审计 + NATS 队列去重

4. **容错机制**
   - 断点续跑：Event Sourcing（NATS JetStream）+ 定期状态快照（TiKV），恢复时加载快照 + 重放后续事件
   - 冲突检测与解决：Intent-based locking（NATS 广播编辑意图）→ 四级解决管线（auto-merge → LLM-assisted → 重新分配 → 人工介入）
   - 索引一致性：webhook/polling 多层触发 + 定期审计校验 + NATS exactly-once 消费
   - LLM API 容错：指数退避 + jitter 重试、Token Bucket 双维度限流、优先级队列、Opus→Sonnet→Haiku 模型降级链、Circuit Breaker

5. **代码执行安全模型**
   - MVP：本地直接执行（快速迭代）
   - 架构预留：Sandbox trait（文件系统隔离、网络隔离、资源限制）
   - 生产期：Docker/gVisor 容器沙箱实现 Sandbox trait

### 技术架构

6. **Rust + Python 混合架构**
   - Rust 核心：索引引擎、调度器、检索引擎、Memory 存储客户端
   - Python Agent 层：Orchestrator（LLM 分解/聚合）、Worker（LLM 执行/工具调用）
   - 桥接：EngineApi trait → LocalEngine（PyO3 FFI）+ GrpcEngineClient（tonic），运行时切换
   - Async：pyo3-async-runtimes 暴露 Python coroutine，sync wrapper 释放 GIL

7. **通信协议**
   - gRPC + Protocol Buffers：同步请求（任务分配、Memory 读写、检索查询）
   - NATS JetStream：异步事件流（状态变更、索引更新、Worker 心跳、断点续跑事件）

8. **存储层**
   - TiKV：短期 Memory、任务状态、快照、文件锁、缓存
   - Qdrant：语义检索索引、长期 Memory embedding、AST 分块向量
   - PostgreSQL：结构化元数据（repos、symbols、references、Agent 注册、任务定义、索引状态）

## Acceptance Criteria

* [x] Rust workspace 结构搭建完成（5 crates + Python package）
* [x] EngineApi trait 定义 + LocalEngine / GrpcEngineClient 实现
* [x] PyO3 binding 可从 Python 调用核心引擎（local mode）
* [x] gRPC server 可独立启动并响应检索/Memory 请求
* [x] Orchestrator 可接收任务 → 分解 → 分配 → 聚合结果
* [x] Worker 可执行子任务（调用 LLM + 工具）并汇报
* [x] 短期 Memory 读写（in-memory fallback; TiKV client coded, needs infra）
* [x] 长期 Memory 读写 + 语义检索（in-memory fallback; Qdrant client coded, needs infra）
* [x] 文本检索跨仓库可用（语言感知分词）
* [x] AST 检索可用（tree-sitter → PostgreSQL symbols/references）
* [x] 增量索引管线可用（git diff → 更新索引）
* [x] 断点续跑可用（快照 + 事件重放恢复）
* [x] 冲突检测可用（Intent broadcast + 三方 merge）
* [x] LLM API 限流处理可用（退避 + 降级）

## Definition of Done

* Tests added/updated（Rust unit/integration + Python unit）
* `cargo clippy` + `cargo test` + `pytest` green
* Docs/architecture.md 更新
* CI pipeline 配置完成（GitHub Actions: ci-rust.yml + ci-python.yml）
* Docker Compose 开发环境可用

## Technical Approach

### 项目结构

```
ultimate-coders/
├── Cargo.toml                    # Workspace root
├── pyproject.toml                # Maturin build config
├── crates/
│   ├── uc-types/                 # 共享类型（EngineApi trait, EngineError, 数据结构）
│   ├── uc-engine/                # 核心引擎（LocalEngine, indexer, memory, scheduler）
│   ├── uc-grpc/                  # gRPC server/client + proto 定义
│   ├── uc-grpc-server/           # 独立 gRPC server 二进制
│   └── uc-python/                # PyO3 Python binding
├── python/
│   └── ultimate_coders/          # Python 人体工学层
│       ├── engine.py             # create_engine() 工厂
│       ├── agent/
│       │   ├── orchestrator.py   # Orchestrator（LLM 分解/聚合）
│       │   └── worker.py         # Worker（LLM 执行）
│       ├── search/               # SearchQuery builder
│       ├── memory/               # Memory 读写接口
│       └── config.py             # TOML/YAML 配置加载
├── proto/                        # protobuf 定义
├── tests/
│   ├── rust/                     # Rust 测试
│   └── python/                   # Python 测试
└── docs/
```

### 关键技术细节

1. **EngineApi trait**（uc-types）：统一接口，LocalEngine 直接调用核心组件，GrpcEngineClient 通过 tonic 调用远程服务
2. **PyO3 async**：pyo3-async-runtimes（v0.22+）桥接 tokio/asyncio，sync wrapper 用 `py.allow_threads()` 释放 GIL
3. **AST 索引**：tree-sitter 解析 → symbols/references 写入 PostgreSQL，支持函数/类/调用链结构化查询
4. **Embedding**：Voyage Code 3（MVP，API 调用）→ UniXcoder/StarCoder2（Phase 2，自托管）
5. **Event Sourcing**：所有 Agent 动作/观察写入 NATS JetStream，定期快照到 TiKV，恢复时加载快照 + 重放
6. **冲突解决**：Intent broadcast → 三方 auto-merge（~70%）→ LLM-assisted（~90%）→ 重新分配 → 人工
7. **LLM 限流**：Token Bucket（RPM + TPM 双维度）+ 优先级队列 + Opus→Sonnet→Haiku 降级 + Circuit Breaker

## Decision (ADR-lite)

| # | Context | Decision | Consequences |
|---|---------|----------|-------------|
| 1 | Agent 协作模式 | Orchestrator-Worker | 控制流清晰，Orchestrator 为单点需容错 |
| 2 | 共享 Memory | 分层（短期 TiKV + 长期 Qdrant） | 灵活可扩展，需设计分层读写策略 |
| 3 | Git 检索深度 | 混合（Text + Semantic + AST） | 最全面，需索引管线 + embedding + AST 解析 |
| 4 | 技术栈 | Rust 核心 + Python Agent | 性能极致，开发周期长 |
| 5 | Rust-Python 桥接 | PyO3 FFI + gRPC 双模 | 单机低延迟 + 集群可扩展，需统一接口抽象 |
| 6 | 存储 | TiKV + Qdrant + PostgreSQL | 全 Rust 栈一致性，TiKV 运维较重 |
| 7 | 执行安全 | MVP 无沙箱 + 预留 Sandbox trait | MVP 最快，Sandbox trait 必须设计时就位 |
| 8 | 通信 | gRPC 同步 + NATS 异步 | 各取所长，需维护 proto + NATS subject 规范 |

## Out of Scope (explicit)

* Agent 角色特化系统（前端/后端/测试角色）— 后期迭代
* 多 LLM 提供商支持 — MVP 先支持 Claude API
* 非代码资源检索（文档/Issue/PR）— 后期扩展
* Web UI / Dashboard — MVP 先 CLI + API
* LSP-at-scale 精确引用 — Phase 2（MVP 用 tree-sitter）
* 自托管 embedding 模型 — Phase 2（MVP 用 Voyage Code 3 API）
* Windows 平台支持 — 后期

## Research References

* [`research/rust-python-bridge.md`](research/rust-python-bridge.md) — PyO3 + gRPC 双模桥接模式、async 运行时桥接、monorepo 结构
* [`research/code-indexing-pipeline.md`](research/code-indexing-pipeline.md) — Sourcegraph/Zoekt/GitHub Blackbird 架构、tree-sitter AST、Voyage Code 3 embedding、索引一致性
* [`research/agent-orchestration.md`](research/agent-orchestration.md) — OpenHands 事件流架构、Event Sourcing 断点续跑、Intent-based locking 冲突解决、LLM 限流降级

## Technical Notes

* 当前仓库为空，从零开始
* PyO3 >= 0.22（Bound API + pyo3-async-runtimes），Python >= 3.9
* Rust future 跨 FFI 必须是 Send + 'static，不可借用 Python 引用
* gRPC streaming 从 PyO3 调用需包装为 Python async generator
* TiKV 存储倒排索引需原型验证（公开文档较少）
* Embedding 模型选择应先用项目代码样本验证质量
* NATS subject 命名规范：`task.assign.{worker_id}`, `task.result.{task_id}`, `agent.events`, `agent.checkpoints`, `index.update.{repo_id}`