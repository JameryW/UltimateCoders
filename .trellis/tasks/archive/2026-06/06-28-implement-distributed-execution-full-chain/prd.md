# PRD: 实现分布式执行全链路能力

## 问题

5个关键缺口阻止分布式执行全链路运行：

1. **Decomposer 本地执行** — `decompose()` 调用 `runSubprocess()`，不走 worker
2. **Supervisor Review 本地执行** — `reviewSubtask()` 调用 `runSubprocess()`，不走 worker
3. **无本地 fallback** — NATS 不可用时 gRPC submit_task 直接失败
4. **dispatchMode 被忽略** — `executeSubtask()` 硬编码走 remote
5. **单 worker 单点** — 只有一个 worker 实例

## 解决方案

### 1. dispatchMode 路由决策（核心）

修改 `executeSubtask()` 根据 dispatchMode + worker 可用性路由：
- `local` → 本地 `runSubprocess()`
- `remote` → 远程（无 worker 则失败）
- `prefer_remote` → 远程优先，无 worker 降级本地

### 2. Decomposer/Supervisor 远程化

将 `decompose()` 和 `reviewSubtask()` 封装为特殊 subtask，通过 NATS 派发给有 `decompose`/`review` 能力的 worker。保留本地 fallback（`dispatchMode: "prefer_remote"`）。

### 3. 本地 Fallback

gRPC `submit_task` 端：NATS 不可用时，通过 `LocalEngine.submit_task()` 本地执行（已有 `InMemoryTaskBackend`）。

### 4. 多 Worker 部署

docker-compose 支持 `--scale worker=N`，worker 自动注册能力。

## 实现范围

| 文件 | 改动 |
|------|------|
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | executeSubtask 路由 + decompose/review 远程化 |
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | listWorkers 查询 |
| `crates/uc-grpc/src/server.rs` | submit_task 本地 fallback |
| `python/ultimate_coders/nats_worker.py` | worker 注册 decompose/review 能力 |

## 不做

- NATS cluster 高可用（运维配置，非代码）
- LSP 远程化（需要额外架构设计）
