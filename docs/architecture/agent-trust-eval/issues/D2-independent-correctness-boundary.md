# D2-independent-correctness-boundary

Status: **decided** (2026-10-24 — intake round-1 via「继续」adopting Proposed)
Type: grilling
Blocked by: _(none)_
Blocks: P0 tickets closing F8 remainder; any “trust agent self-report” removal

## Question

「正确性」的信任边界画在哪里：Agent 不可写环境里的**独立黄金参考 / UC 自有 verifier**，还是「harness 指纹 + 出处 attestation」就够？Agent 生成的 `correctness_harness.py` 是否仍算信任边界之内？

## Context

- F8 状态「部分」：剩余缺口是 harness 自报 `correctness`（`docs/agent-os-architecture.md`）。
- Oracle 已把 `BenchmarkResult.correctness` 当硬门；runner 在 `not correctness` 时短路。
- 用户 P0：「评测放到 Agent 改不到的环境里重跑，不信它的自报。」

## Resolution

**双闸（attestation + 独立重跑）。**

1. Protected harness 指纹 / 出处 attestation **仍然需要**（保留现有 fail-closed 指纹路径）。
2. Oracle **接受前**必须在 Agent **不可写**路径再跑一次正确性检查；Agent 工作区自报不足以关闭正确性门。
3. Agent 生成的 `correctness_harness.py` **不得单独**作为接受依据；可作候选输入，最终以独立重跑为准。
4. 完全自研 golden verifier 可分阶段交付；功能开关默认要求独立重跑。此决议用于关闭 F8 剩余缺口的产品口径。
