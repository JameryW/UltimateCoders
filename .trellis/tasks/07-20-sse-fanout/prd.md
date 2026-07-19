# PRD: SSE Fan-out（#8，最后 MED）

## 背景

/loop 第 26 轮，Python 审计 #8（MED，剩项最后一个 MED）。dashboard 单 `_nats_event_queue` 被所有 `/dashboard/api/stream` 客户端竞争 `queue.get()`：

1. **多标签页各得随机子集**——事件被负载均衡到某一个客户端，多开 dashboard 各看一部分。
2. **event_id 计数分歧**——SSE resume 无意义。
3. **`_event_log.appendleft` + `_metrics.record_event` 写在客户端消费循环里**——零客户端挂载时 REST `/events` 与 metrics **完全丢事件**；多客户端时重复记录（每客户端记一次）。

## 改

1. `DashboardApp`：`_sse_subscribers: set[asyncio.Queue]`（__init__ 建 set 无 loop 绑定，Py3.9 安全）；新 `_subscribe_sse()`（建 per-client maxsize=1000 queue 入集）/`_unsubscribe_sse(q)`（discard）。删 `_get_nats_event_queue` + `_nats_event_queue` 字段（死码）。
2. `_handle_nats_event`：解析后**记录一次**（`_event_log.appendleft` + `event_emitter._recent` + `_metrics.record_event`——从客户端循环移入）+ 广播 `put_nowait` 到每个 subscriber queue（满 → warning 丢该客户端，不影响他人——per-client backpressure）。
3. stream generator：进入时 `_subscribe_sse()`（运行 loop 内建 queue），`try/finally` 保证 `_unsubscribe_sse`（sse-starlette 客户端断开 cancel generator 也不泄漏）；消费自己的 queue；删除循环内记录代码。

## 验收

- test_dashboard_stream.py：
  - fan-out：两 subscriber 各得全部 3 事件同序；metrics/event_log 各记 3 次（非 6）。
  - 零客户端：事件仍入 event_log + metrics（REST /events 不再丢）。
  - 满队列：drop + warning，不影响他客户端（既有测试改新 API）。
  - 既有 snapshot/shape 测试改 `_subscribe_sse` patch，全绿。
- pytest tests/python 全绿 + ruff exit 0；feature branch + PR + CI green。

## 不做

- #9 file broadcast 大小上限（下轮）；#11 JetStream replay 死码；#15 PTY 线程泄漏 + SearchQuery 校验。清完 #8 后审计剩 3 项 LOW-MED。
