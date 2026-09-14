# T5: 统一 Executor trait（JetStream 硬依赖 / effect_class 白名单 / 分解不再随 NATS 分叉）

- Issue: #641
- Blocked by: T3（已落地）, D4 #633（decided 2026-09-11）
- Design refs: D4 全部三点（`.scratch/durable-runtime-migration/gh-D4.md`），§3 G5
- 日期: 2026-09-14

## Goal

建立 uc-engine 统一执行器抽象 `execute(envelope) -> AttemptOutcome`，并落实 D4 的三项运输收敛：
JetStream 成为 subtask 派发的唯一路径（core NATS 回退删除）、LocalExecutor 按 effect_class
白名单降级、Rust 侧分解回退分叉删除（分解权归属 TS planner，D2）。

## Scope

1. **uc-types**：`EffectClass` 枚举（read_only / local_safe / requires_worker，default
   requires_worker，serde snake_case 与 graph_nodes 列值一致）；`Subtask.effect_class`
   （serde default）；删除 `DispatchMode::Local`（全仓无构造点，no-op 变体）。
2. **uc-engine**：新 `scheduler/executor.rs`——`Executor` trait + NatsExecutor（JS 硬依赖，
   无 core 回退）/ LocalExecutor（注入 handler）/ SandboxExecutor / RemoteExecutor（显式
   占位）；`ExecutorSelector` 路由（Remote 永不本地；NATS 不可用时仅 ReadOnly/LocalSafe →
   Local，RequiresWorker → StayReady + 告警）。**旁路挂载**：不改 legacy dispatch 行为
   （T6 切主）。删除 `decompose_task` / `steps_for_description` / `strip_workflow_marker`
   及其单测；legacy `TaskStore::submit_task` 改 insert-only。
3. **uc-grpc**：删两处 `DispatchMode::Local` no-op 分支 + conversions arms；
   `subtask_execute_payload` 增 effect_class（additive）；修正 TaskStore stale 注释。
4. **uc-grpc-server**：启动时 get-or-create UC_SUBTASKS 流（workqueue/7d/120s dedup，
   参数镜像原 worker 侧配置）。
5. **python worker**：删 core NATS queue group 回退；UC_SUBTASKS 流创建移除（gateway
   供给）、consumer ensure fail-fast；JS 不可用 → 周期重试 + 就绪前拒注册 + 
   `subtask_transport` 状态进 registration metadata 与心跳（health/Dashboard 可见）；
   types.py 删 LOCAL、payload 解析删 local 映射、orchestrator loop LOCAL 死分支删除。
6. **graph_store**：`NodeRow` 增 `effect_class`（读列 + 投影写入，default requires_worker）。

## Test seams

- NATS-down 集成测试（uc-engine）：local_safe 图继续推进（LocalExecutor 执行 outcome）、
  coding 节点停 READY + 告警事件、WHAT 字节级不变（路由不触碰 description/steps/depends_on）。
- JS 不可用拒注册（pytest）：测试替身必须镜像真实 JS 客户端行为（stream 缺失时
  pull_subscribe 抛错），断言：不注册 gateway、周期重试、`subtask_transport` 状态可观测。
- 无头 grep（check-phase 手工执行并记录进关票）：`decompose_task`、`DispatchMode::Local`、
  subtask 回退 `queue="workers"`、`Falls back to core NATS` 生产代码零匹配。

## 门禁

- fmt / clippy `-D warnings`：uc-types、uc-engine（默认 + --no-default-features）、
  uc-grpc --all-features、uc-grpc-server。
- Rust 测试基线（T4 后，只增不减）：437 / 379 / 192+8 / 36 / 34。
- pytest 全量（默认 TMPDIR 跑法；merge_arbiter/workspace 两文件 bypass + 浅 basetemp 单跑）。

## Out of scope

- 生产 LocalExecutor 的真实工具执行体（随 T6/T7）；dispatch mouth 切换到 Executor trait
  （T6）；事件/控制面收敛（D4 Q3）；TS planner 侧 effect_class 产出。
