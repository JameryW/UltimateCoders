# PRD: Dashboard NATS 连接移到服务事件循环（#5 HIGH）

## 背景

/loop 第 23 轮，Python 审计 #5（HIGH/M）。dashboard 两处（`__main__._connect_nats` L77-103、`app.from_env` ~L1490）在**临时 event loop** 上 `nats_lib.connect()`，finally `loop.close()`。nats-py 的内部 reader/ping 任务建在那个 loop 上——关 loop 即杀之。随后 `_subscribe_nats_events` 的 startup hook 在 **uvicorn 的 loop** 上 `subscribe()`，transport 绑死 loop → **消息永不投递**（publish 抛 "attached to a different loop" 仅 warning）。净效果：独立 dashboard 的 SSE/MetricsAggregator **收不到任何 uc.task.event**，而启动日志报 "Connected to NATS"、CLI banner 报 "NATS: connected"——谎报健康。同 Py3.9 asyncio-in-__init__ bug 类（但版本无关）。

## 改

连接移入 uvicorn loop（startup hook 内）：

1. `DashboardApp.__init__` 加 `nats_url: str | None = None` 参数（与既有 `nats_client` 并存——测试/嵌入式调用方注入已连接客户端的路径不变）。
2. 新 `async _connect_and_subscribe_nats()`（startup hook 调）：`_nats_client` 已注入 → 跳过连接直接订阅；否则有 `_nats_url` → `asyncio.wait_for(nats_lib.connect(url, connect_timeout=5, max_reconnect_attempts=0), timeout=6)` + flush（现在在活 loop 上，reader/ping 任务存活）→ 记 `_owns_nats_client = True`；失败 → warning "running snapshot-only"，不抛。然后 subscribe（原 `_subscribe` 内逻辑）。
3. shutdown hook `_close_owned_nats()`：自连的客户端 drain + close（在正确的 loop 上）。
4. `_subscribe_nats_events()` 改注册两个 hook（startup/shutdown）。
5. `__main__.main`：删 `_connect_nats`/`_drain_nats` 辅助函数；`DashboardApp(orchestrator=None, nats_url=None if args.no_nats else args.nats_url)`；信号 handler 去掉 `_drain_nats` 调用（shutdown hook 接管，app.stop() 触发）；banner "NATS: connected" → "NATS: enabled (connects on server start)"/"disabled"（诚实——连接异步发生于 startup）。
6. `from_env`：同改——传 `nats_url` 不再临时 loop 连接。

## 验收

- tests/python 新用例：
  - 死端口 nats_url → `await app._connect_and_subscribe_nats()` 不抛、`_nats_client is None`（snapshot-only 降级）。
  - 注入 fake nats_client（AsyncMock subscribe）→ `_connect_and_subscribe_nats()` 订阅到 NATS_SUBJECT_TASK_EVENT（跳过连接）。
- 既有 test_dashboard_stream.py 全绿（nats_client 注入路径不变）。
- pytest tests/python 全绿 + ruff clean（**输出完整读，勿 tail -1**——round 22 教训）。
- feature branch + PR + CI green。

## 不做

- #6 同步 gRPC 阻塞事件循环（下轮 MED）；#8 SSE 单队列 fan-out（MED，独立）；#9-#15 杂项。
