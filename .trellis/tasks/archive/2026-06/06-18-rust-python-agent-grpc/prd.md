# 完善后端实现: Rust引擎+Python Agent+gRPC

## Goal

补全 UltimateCoders 后端三大层的已知 gap、新增多 LLM provider 支持、TaskStore 持久化、Python 测试覆盖、EventStore 统一，使后端从"功能完整但 fragile"升级为"生产可用"。

## Requirements

### R1: 补全已知 Placeholder (7 项)

1. **ConflictResolver._llm_assisted_merge()** — 实现 LLM 辅助合并，不再返回 `success=False`
2. **ConflictResolver._auto_merge()** — 实现真正的三路 diff 合并，而非生成 conflict markers
3. **config.load_config()** — 支持 TOML/YAML 文件加载，不仅读环境变量
4. **NatsWorker._execute_subtasks()** — 改用公共 API `select_next_subtask()` 而非私有 `_select_next_subtask()`
5. **Worker._collect_modified_files()** — 追踪 diff 内容，不再始终为空字符串
6. **AgentAdapter 基类** — 加 `ABC`/`@abstractmethod` 强制约束
7. **CORS 加固** — `AllowOrigin::Any` → 可配置的 origin 列表

### R2: 多 LLM Provider 支持

- 采用 **litellm 委托模式**（Aider/OpenHands 路线），最小代码量
- 重构 `LLMClient` 为 `LiteLLMClient`，统一 tool calling 格式为 OpenAI 格式
- `LlmConfig.provider` 字段激活，支持 `anthropic/openai/gemini/deepseek` 等
- 保留 Anthropic 原生路径作为 fallback（litellm 不可用时）
- Streaming 抽象：`GenericStreamingChunk` 统一接口

### R3: TaskStore 持久化

- 定义 `TaskStoreBackend` trait（在 uc-engine），遵循 `EventStore` trait 模式
- 实现 `InMemoryTaskBackend`（当前 HashMap 逻辑迁移）
- 实现 `PostgresTaskBackend`（遵循 `PostgresMetadataStore` 双路径模式）
- `uc-grpc` 通过 `Arc<dyn TaskStoreBackend>` 消费，feature-gated
- PostgreSQL schema: `tasks` 表（当前状态）+ `agent_events` 表（append-only 审计日志）
- 无需数据迁移（当前无持久化）

### R4: EventStore 与 TaskStore 事件统一

- 当前两套事件系统：`TaskStore.events: Vec<AgentEventType>`（inline，WatchTask 用）vs `EventStore` trait（uc-engine，NATS 持久化）
- 统一为：`EventStore` trait 为唯一事件源，`TaskStore` 通过 `Arc<dyn EventStore>` 写入/读取事件
- `WatchTask` streaming 从 `EventStore.read_from()` 读取，不再从 inline Vec
- `CheckpointManager` 快照写入 PostgreSQL 而非 DashMap

### R5: Python 测试覆盖

- 创建 `conftest.py`，提取公共 fixtures（`_make_engine` 等）
- 实现 `StubEngine`（纯 Python，Protocol-based），无需 Rust 扩展即可测试
- `pytest.importorskip` 守护需要 Rust 扩展的测试
- `pytest.mark.integration` 标记集成测试
- Mock `LLMClient`（Anthropic API calls）和 `SandboxManager`（subprocess）
- 配置 `pytest-cov`，排除 `_uc_core` native extension

## Acceptance Criteria

- [ ] 所有 7 个 placeholder 补全，无 `TODO`/`placeholder`/`NotImplemented` 残留
- [ ] `LLMClient` 支持 3+ provider（anthropic/openai/gemini），tool calling 正常
- [ ] `TaskStore` 可配置为 in-memory 或 PostgreSQL，重启后状态恢复
- [ ] `EventStore` 为唯一事件源，`WatchTask` streaming 从 EventStore 读取
- [ ] Python 测试覆盖率 ≥ 60%（agent/ 模块）
- [ ] `cargo test` + `pytest` 全绿
- [ ] CI green

## Definition of Done

- Tests added/updated (Rust unit + Python unit + integration)
- Lint / typecheck / CI green
- Docs/notes updated if behavior changes
- Rollout/rollback considered (feature flags for TaskStore persistence, litellm opt-in)

## Technical Approach

### 多 LLM Provider: litellm 委托

```
LLMClient (现有)
  → LiteLLMClient (新)
    → litellm.acompletion(model="provider/model-name", tools=OpenAI_format, ...)
    → 返回 OpenAI-format ModelResponse
    → 内部翻译 tool calls 回 AgentAction 格式
  → AnthropicLLMClient (fallback, litellm 不可用时)
    → 现有 anthropic SDK 直接调用
```

### TaskStore 持久化: Swap-In Replacement

```
TaskStore (现有, uc-grpc)
  → TaskStoreBackend trait (新, uc-engine)
    → InMemoryTaskBackend (HashMap, 迁移自现有)
    → PostgresTaskBackend (sqlx, feature-gated)
  → uc-grpc 通过 Arc<dyn TaskStoreBackend> 消费
```

### EventStore 统一

```
TaskStore.events: Vec<AgentEventType> (现有, inline)
  → 删除 inline events
  → TaskStore 持有 Arc<dyn EventStore>
  → 写事件: event_store.append(subject, event)
  → 读事件: event_store.read_from(subject, offset)
  → WatchTask: 从 EventStore 读取 + broadcast channel 实时推送
```

## Decision (ADR-lite)

**Context**: 后端功能完整但有 placeholder、单 provider、无持久化、无测试
**Decision**: litellm 委托模式（非自建抽象层）；swap-in TaskStore 持久化（非 dual-write）；EventStore 统一（删除 inline events）
**Consequences**: litellm 依赖较重但省大量代码；TaskStore 迁移需改 uc-grpc 依赖；EventStore 统一需改 WatchTask 实现

## Out of Scope

- 分布式锁 / multi-region TaskStore
- LLM provider-specific 高级特性（Anthropic prompt caching、Gemini grounding）
- gRPC 连接池优化（当前单连接足够）
- Rust 层测试覆盖提升（已有较好覆盖）

## Research References

- [`research/multi-llm-provider.md`](research/multi-llm-provider.md) — litellm 委托模式推荐，7 个 Anthropic 耦合点
- [`research/taskstore-persistence.md`](research/taskstore-persistence.md) — swap-in 替换策略，PostgresMetadataStore 双路径模式
- [`research/python-testing.md`](research/python-testing.md) — StubEngine Protocol 模式，conftest.py 提取

## Implementation Plan (PR 序列)

### PR11: 补全 Placeholder + Python 测试基础设施
- 7 个 placeholder 补全
- `conftest.py` + `StubEngine` + `pytest-cov` 配置
- `pytest.importorskip` + `@pytest.mark.integration`
- **风险**: 低，纯补全 + 测试基础设施

### PR12: 多 LLM Provider (litellm)
- `LiteLLMClient` 实现
- `LlmConfig` 激活 provider 字段
- `config.load_config()` TOML/YAML 支持
- Anthropic fallback 路径
- **风险**: 中，litellm 依赖引入 + tool calling 格式转换

### PR13: TaskStore 持久化 + EventStore 统一
- `TaskStoreBackend` trait + `InMemoryTaskBackend` + `PostgresTaskBackend`
- EventStore 统一（删除 inline events）
- PostgreSQL schema + migration
- WatchTask 从 EventStore 读取
- **风险**: 中高，跨 crate 依赖变更 + 数据流重构

## Technical Notes

- 所有 5 个 Rust crates 编译通过，零错误零警告
- Python 层 anthropic/pyyaml 为 soft dependency
- uc-grpc 依赖 uc-engine 时 default-features=false
- uc-grpc-server 是唯一启用 uc-engine 默认 features 的 crate
- TaskStore 在 uc-grpc，无 sqlx 依赖；持久化需放 uc-engine
- 两套事件系统需统一：TaskStore.events (inline) vs EventStore trait (uc-engine)
