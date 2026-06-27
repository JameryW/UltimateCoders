# PRD: Fix OMP Task Interruption — Timeout / Retry / CircuitBreaker

## Problem
OMP 执行任务过程中经常中断，3 个根因：

1. **subtask 硬超时 5min** — `executeSubtask` 用 `AbortSignal.timeout(300_000)`，复杂 coding subtask 经常超时 → cancelled → cascade cancel → task fail
2. **worker 检查无重试** — 每波前 `checkWorkerAvailability` 一次失败就 fail task，gRPC 短暂断连直接误杀
3. **circuit breaker 跨波累积** — 前波失败使 breaker 打开，后续波直接 fail，无法恢复

## Fix

### F1: 可配置 subtask 超时，默认 10min
- `OrchestratorConfig` 加 `subtaskTimeoutMs?: number` (default 600000)
- `executeSubtask` 用 `this.config.subtaskTimeoutMs ?? 600_000` 替换硬编码 300000

### F2: worker 检查加重试
- `executeWaves` 中 `checkWorkerAvailability` 失败后等 5s 重试一次
- 避免单次 gRPC 抖动误杀 task

### F3: wave 间 reset circuit breaker
- 每波结束后 `this.circuitBreaker.reset()`
- 前波失败不拖垮后波

## Scope
- `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` 唯一修改文件
- 3 处定点修改，无架构变更
