# Decouple Orchestrator/Gateway from Distributed Workers

## Goal

将当前紧耦合的单体架构拆分为两个独立部署单元：**Orchestrator/Gateway 服务**（基于 OMP 的编排 + Rust gRPC 网关）和 **Distributed Worker 服务**（分布式编码执行器），使两者可以独立扩缩容、独立部署、独立演进。

## Requirements

1. **Gateway 独立部署** — OMP + Rust gRPC Server 可独立运行，无 Worker 时仍能接受任务、排队、展示 UI
2. **Worker 独立部署** — Python Worker 可独立启动，自动注册到 Gateway
3. **Worker 注册 RPC** — 新增 gRPC WorkerService，Worker 通过 gRPC 注册能力 + 心跳（不依赖 NATS heartbeat）
4. **能力感知调度** — Gateway 根据 Worker 能力（capabilities）+ 负载调度 subtask
5. **编排职责保持现状** — 任务分解留在 Python Orchestrator（通过 NATS）和 OMP orchestrator.ts（本地 DAG）两条路径，不迁移
6. **本地模式保留** — LocalWorkerBridge/JSON-RPC 模式继续工作
7. **NATS 仍是消息中间件** — 任务分发、状态更新走 NATS；Worker 注册走 gRPC

## Acceptance Criteria

- [ ] Gateway 独立启动后，submit_task 可排队等待 Worker
- [ ] Worker 独立启动后，通过 gRPC WorkerService 注册到 Gateway
- [ ] 多 Worker 实例可同时注册，Gateway 正确展示（ListWorkers）
- [ ] Gateway 根据 required_capabilities 匹配 Worker 进行调度
- [ ] Worker 断连后，Gateway 标记其 subtask 为 Failed/Pending（现有 heartbeat 机制）
- [ ] 本地模式（无 NATS）仍可通过 LocalWorkerBridge 执行任务
- [ ] 现有 OMP TUI / Dashboard 功能不受影响

## Definition of Done

- Tests added/updated (unit/integration where appropriate)
- Lint / typecheck / CI green
- Docs/notes updated if behavior changes
- proto 变更向后兼容

## Decision (ADR-lite)

### Decision 1: MVP 范围

**Context**: 需要决定拆分的深度
**Decision**: 核心拆分 + Worker 注册 RPC（选项 2）
**Consequences**: 编排职责不迁移，降低风险；Worker 注册从 NATS heartbeat 升级为 gRPC RPC，更可靠

### Decision 2: 编排职责归属

**Context**: 任务分解（decompose）在 Python 和 OMP 两处都有实现
**Decision**: 保持现状，两条路径并存
- Python 路径: NATS → Python Orchestrator → LLM 分解 → Worker 执行
- OMP 路径: OMP orchestrator.ts → DAG 构建 → runSubprocess 本地执行
**Consequences**: 不引入迁移风险；未来可考虑统一

### Decision 3: Worker 注册协议

**Context**: 当前 Worker 通过 NATS heartbeat 被动发现，可靠性不足
**Decision**: 新增 gRPC WorkerService
- `RegisterWorker(WorkerRegistration) → WorkerRegistrationResponse` — 启动时注册
- `WorkerHeartbeat(WorkerHeartbeatRequest) → WorkerHeartbeatResponse` — 周期心跳
- `DeregisterWorker(DeregisterWorkerRequest) → DeregisterWorkerResponse` — 优雅下线
**Consequences**: Worker 需要同时连接 gRPC（注册）和 NATS（执行）；Gateway 成为 Worker 状态的唯一 source of truth

## Out of Scope

- 编排职责迁移（Python → Gateway）
- 多语言 Worker 支持（Rust/Go Worker）
- Worker 自动扩缩容
- 跨集群调度
- DashboardService 重构

## Technical Approach

### 新增 proto: WorkerService

```protobuf
service WorkerService {
    rpc RegisterWorker(RegisterWorkerRequest) returns (RegisterWorkerResponse);
    rpc WorkerHeartbeat(WorkerHeartbeatRequest) returns (WorkerHeartbeatResponse);
    rpc DeregisterWorker(DeregisterWorkerRequest) returns (DeregisterWorkerResponse);
}
```

### 架构图（拆分后）

```
┌─────────────────────────────────────────────────┐
│  Orchestrator/Gateway (独立部署)                   │
│                                                   │
│  OMP Extension ──gRPC-Web──→ Rust gRPC Server    │
│  (TypeScript)                  ├─ EngineService   │
│                                ├─ TaskService     │
│                                ├─ DashboardService│
│                                └─ WorkerService ★ │
│                                      │            │
│                                  WorkerRegistry   │
│                                  (in-memory)      │
└──────────────────────────┬──────────┬─────────────┘
                           │          │
                    gRPC   │          │ NATS
                  (register)│         │ (dispatch)
                           │          │
┌──────────────────────────┴──────────┴─────────────┐
│  Distributed Workers (独立部署, 可多实例)             │
│                                                     │
│  Python Worker                                      │
│  ├─ nats_worker.py (worker mode)                   │
│  │   ├─ gRPC: RegisterWorker + Heartbeat           │
│  │   └─ NATS: subscribe uc.subtask.execute         │
│  ├─ worker.py (subtask execution)                  │
│  └─ sandbox.py (isolated execution)                │
│                                                     │
│  本地模式: LocalWorkerBridge (JSON-RPC, 不变)         │
└─────────────────────────────────────────────────────┘
```

### 实现计划

**PR1: proto + WorkerService 桩**
- 新增 WorkerService 到 engine.proto
- Rust: GrpcServer 实现 WorkerService RPC（注册、心跳、注销）
- WorkerRegistry struct（内存中，替代 NATS heartbeat 被动发现）
- ListWorkers 改为从 WorkerRegistry 读取（不再 NATS passthrough）

**PR2: Python Worker 注册**
- nats_worker.py worker mode 启动时调用 gRPC RegisterWorker
- 周期 WorkerHeartbeat
- 优雅关闭时 DeregisterWorker
- Python nats_worker default mode 也注册（它也是 Worker）

**PR3: 能力感知调度**
- Rust dispatch_ready_subtasks 根据 Worker capabilities 过滤
- SubtaskProto.required_capabilities 与 WorkerProto.capabilities 匹配
- 无匹配 Worker 时 subtask 保持 Pending（等待 Worker 上线）

**PR4: Docker 独立部署 + 集成测试**
- docker-compose: gateway + worker 分离
- 集成测试: 多 Worker 注册 + 调度
- 本地模式回归测试

## Technical Notes

### 关键文件

- `crates/uc-grpc/proto/engine.proto` — 新增 WorkerService
- `crates/uc-grpc/src/server.rs` — GrpcServer 实现 WorkerService
- `crates/uc-grpc/src/local_worker.rs` — LocalWorkerBridge (不变)
- `python/ultimate_coders/nats_worker.py` — 新增 gRPC 注册
- `python/ultimate_coders/agent/worker.py` — Subtask 执行 (不变)
- `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` — OMP bridge (不变)

### 现有 Spec

- `local-worker-bridge-spec.md` — JSON-RPC 2.0 bridge
- `nats-bridge-spec.md` — NATS 消息协议
- `taskservice-grpc-spec.md` — TaskService gRPC
- `agent-capability-spec.md` — Worker 能力模型

### 当前编排路径分析

存在两条独立的编排路径：

1. **OMP 本地路径**: OMP orchestrator.ts → DAG + wave 调度 → runSubprocess
   - 不经过 NATS，不经过 Rust TaskStore
   - subtask 执行使用 OMP 的 coding agent
   - 适用于本地开发场景

2. **Python NATS 路径**: Rust submit_task → NATS → Python nats_worker → Orchestrator.decompose → Worker.execute
   - 通过 NATS 消息驱动
   - Python Worker 执行 subtask（Sandbox 隔离）
   - 适用于分布式场景

拆分主要影响路径 2；路径 1 基本不变。
