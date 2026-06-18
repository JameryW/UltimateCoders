# Dashboard Polish — lastUpdate, SSE reliability, Header fix

## Goal

修复 Dashboard 的几个中低优先级但影响体验的问题：Header 时间戳缺失、SSE 断连检测延迟、SSE 事件缺少 id/heartbeat。

## What I already know

* Header 组件接受 `lastUpdate` prop 但 App.tsx 从未传入 — 时间戳永远空白
* SSE `event_generator()` 中 `asyncio.sleep(2/5)` 阻塞断连检测，死连接最多保持 5s
* SSE 事件缺少 `id` 字段，浏览器 EventSource 无法高效 resume
* 无 SSE heartbeat comment，若 snapshot 生成慢可能导致浏览器超时断连

## Requirements

1. Header 显示最后更新时间戳
2. SSE 断连检测更快（sleep 期间也能检测）
3. SSE 事件带 `id` 字段（单调递增计数器）
4. SSE heartbeat comment 防止浏览器超时

## Acceptance Criteria

- [ ] Header 显示最后更新时间（来自 SSE snapshot 或 gRPC healthCheck）
- [ ] SSE sleep 期间检测到客户端断连后立即退出循环
- [ ] SSE 事件包含 `id` 字段
- [ ] SSE 每 15s 发送 heartbeat comment

## Out of Scope

* Python gRPC subscriber（前端 gRPC-Web 已覆盖 no-NATS 场景）
* 全局 InteractionLog 面板
* 版本号/uptime 显示
