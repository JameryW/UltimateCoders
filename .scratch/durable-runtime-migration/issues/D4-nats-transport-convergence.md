# D4-nats-transport-convergence

Status: decided 2026-09-11 (GitHub #633; full resolution in comment https://github.com/JameryW/UltimateCoders/issues/633#issuecomment-5634917986)
Blocks: T1, T5

## Question
Subtask 派发是否收敛为 JetStream-only（Core queue-group 降级为纯事件广播）；NatsExecutor 不可达时 fallback 语义确认（换 Executor 实现，不换协议）

## Context
See docs/architecture/durable-runtime-migration-assessment.md §4 (and §2.4 for NATS facts; §3 evidence paths).

## Resolution

1. **JetStream-only 硬依赖**：删除 worker Core 静默降级（nats_worker.py L486–503）；JS 不可用→拒绝注册+周期重试+健康/Dashboard 可见；UC_SUBTASKS 流创建归 gateway 侧供给。
2. **LocalExecutor fallback 按 effect_class 白名单**：仅 `local_safe` 节点可在 NatsExecutor 故障时本地执行；Coding 节点保持 READY 排队+告警，不重分解、不改协议（WHAT 冻结，只降 HOW/WHERE）。
3. **P0 只收敛派发面**：事件/控制通道（uc.task.update / uc.task.event / heartbeat）维持 core；node/attempt 级 cancel 语义留给 T7 票面。

喂给：T1、T4（ack/max_deliver 假设单一 JS 路径）、T5。
