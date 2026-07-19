# PRD: Worker Heartbeat Liveness — CRITICAL 误判重派 + 心跳失败不可见

## 背景

/loop 第 19 轮（用户选向 Python worker 审计）。新审计 15 finding，本轮取最高危簇：#1 CRITICAL + #7 MEDIUM（同主题：心跳/liveness 信号）。#2-#6/#8-#15 后续轮。

## Bug 清单（已核实）

### F52: send_heartbeat 零调用方 → 任何 >90s subtask 执行中被重派（审计 #1，CRITICAL，S）

`Worker._last_heartbeat_at` 构造时设一次（worker.py:307），唯一刷新点 `Worker.send_heartbeat()`（worker.py:1568）**全代码库零调用方**（grep 核实）。default 模式 `_stale_worker_cleanup_loop`（nats_worker.py:1930-1957）算 `stall_gap = now - worker._last_heartbeat_at`，>90s 且 `current_task` 非空 → 置 PENDING + retry_count++ + 清 current_task + 唤醒 dispatch。worker 启动 ~90s 后该值永旧 → **每个 >90s 的 subtask（coding agent 常态 5-10 分钟，timeout 600s）执行中被判停滞重派**——原 sandbox 执行继续跑，dispatch 并发重跑同一 subtask：双 git worktree 双写、双 NATS 结果事件、双 handle_subtask_result。

注意：heartbeat tick 刷的是 `orchestrator.refresh_heartbeat(worker_id)`（Orchestrator 侧 WorkerEntry——另一对象），stall 检测读的是本地 Worker 的 `_last_heartbeat_at`——二者不通。

修：`_heartbeat_loop` tick **顶部**（NATS publish 之前）`await self._worker.send_heartbeat()`——"事件循环在推进"即 liveness 信号（publish 失败不该算 worker 死）。send_heartbeat 轻量（刷时间戳 + 返回 info dict）。

### F53: gateway 心跳失败不可见（审计 #7，MEDIUM，S）

`worker_heartbeat_async` 吞所有异常 debug 日志返回 False（engine.py:1110-1131）；heartbeat loop **不查返回值**。gateway 不可达/注销 worker → worker 本地照跑，gateway WorkerRegistry 丢弃它 → dispatch 不再匹配 → 无任何 INFO/WARNING。注册重试仅在 `_grpc_reg_engine is None` 时（启动失败场景），注册后心跳持续失败永不重注册。

修：`_consecutive_heartbeat_failures` 计数（init 0）；tick 内查返回值——True 清零，False 累加，==3 → WARNING 日志 + `_grpc_reg_engine = None`（下 tick 走既有重注册路径 L1356-1359）。

## 验收

- tests/python 新用例（复用 test_nats_worker_helpers 的 `_make_worker()` 无 IO harness）：
  - F52：NatsWorker 挂 mock worker（AsyncMock send_heartbeat），短暂运行 `_heartbeat_loop`（tick 体在 30s sleep 前执行）→ send_heartbeat 被 await。
  - F52：真实 Worker.send_heartbeat() 刷新 `_last_heartbeat_at`（构造器无 IO 可直建）。
  - F53：预设计数 2 + grpc mock 返回 False → 单 tick 后 `_grpc_reg_engine is None`；返回 True → 计数清零。
- pytest tests/python 全绿（本地能跑的子集；CI ci-python 权威）。
- feature branch + PR + CI green。

## 不做（后续轮，按审计排序）

- **#2**（HIGH/S）：NATS callback await 执行体 → 实际并发=1（max_capacity=3 死码）+ 超载消息静默丢（subtask 永失）。改瘦 callback + `_spawn_bg` 派发。
- **#3**（HIGH/M）：default 模式消费自身 uc.task.event 回环 → 结果双处理、merge 仲裁双触发（重复 merge 进 main）、load 下溢。修：事件带 consumer_id 自跳过 或 handle_subtask_result 幂等 + 仲裁 scheduled 标志。
- **#4**（HIGH/M）：remote 分配 subtask 无超时 → 结果事件丢失永卡 ASSIGNED。
- **#5**（HIGH/M）：dashboard NATS 客户端绑临时 event loop 随即关闭 → 消息永不达（日志谎报 connected）。
- **#6**（MED/M）：同步 gRPC engine 调用阻塞事件循环（心跳 30s 失速 → 假性驱逐）。
- **#8-#15**：SSE 单队列竞争、file_changes 无大小上限、shutdown 弃单无报、JetStream replay 死码、错误日志 id 错、importance 0.0→0.5、/metrics 无鉴权、PTY 线程泄漏/SearchQuery 校验。
