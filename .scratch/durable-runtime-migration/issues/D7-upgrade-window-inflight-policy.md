# D7-upgrade-window-inflight-policy

Status: decided 2026-09-11 (GitHub #636; full resolution in comment https://github.com/JameryW/UltimateCoders/issues/636#issuecomment-5634986283)
Blocks: T6 release notes

## Question
D2 反转 + D3 lockstep 升级窗口内，存量 in-flight 任务：排空门禁 / 一次性导入后暂停 / 声明可丢弃

## Context
See docs/architecture/durable-runtime-migration-assessment.md §4 (and §2.4 for NATS facts; §3 evidence paths).

## Resolution

1. **自动续跑**：一次性导入后，已 commit node 保留；窗口内 RUNNING attempt 超时→fence→node 回 READY→新信封重派（D1/D6 恢复模型本身，零新机制）。半程未 commit 工作从零重跑为已接受代价；无排空门禁、无丢弃、无人工暂停闸门。
2. **滞留旧信封消息**：新 worker 直接 term 丢弃 + `stale_dispatch_dropped` 计数（gateway 健康/Dashboard 可见），不建 DLQ——权威重派来自 READY 节点而非队列。
3. **T6 发布说明四要点**：lockstep 升级顺序（旧 worker 因握手自动拒入）；未 commit 工作会重跑、commit 结果永不丢；stale-drop 计数位置；回滚=旧镜像+重导入（D2 约束 2）。
