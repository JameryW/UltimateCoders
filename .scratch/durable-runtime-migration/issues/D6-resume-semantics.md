# D6-resume-semantics

Status: decided 2026-09-11 (GitHub #635; full resolution in comment https://github.com/JameryW/UltimateCoders/issues/635#issuecomment-5634964673)
Blocks: T6

## Question
ready-node 下 pause/resume 的恢复单元：按 node/scope checkpoint 还是整图重算？checkpoint 存哪里（现 .uc/checkpoints 按 wave 边界）

## Context
See docs/architecture/durable-runtime-migration-assessment.md §4 (and §2.4 for NATS facts; §3 evidence paths).

## Resolution

1. **node 态重算**：pause=图级"停派新节点"闸（沿用 Rust 现行为）；resume=从已 commit 的 NodeCompletion 重推 READY；不存图快照；TS `resumeFromWave` + `.uc/checkpoints` wave 快照随 T6 删除，checkpoint 序列退化为 execution_events 审计流（D1）。
2. **软暂停 + 宽限硬停**：RUNNING attempt 在 pause 后照常跑，迟到结果可 commit（T4 fencing 保护）；`UC_PAUSE_GRACE_SECS`（默认 120s）超时后升级为 node 级 cancel（机制归 T7，pause 是首个调用方）。
3. **grace-cancel 不终态化 node**：fence 掉 attempt（epoch bump）、node 回 READY，resume 后可重派——该"cancel-attempt-keep-node"原语加入 T7 验收。
