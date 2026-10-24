# D2-independent-correctness-boundary

Status: open (decision ticket — not implementation)
Type: grilling
Blocked by: _(none — frontier)_
Blocks: P0 tickets closing F8 remainder; any “trust agent self-report” removal

## Question

「正确性」的信任边界画在哪里：Agent 不可写环境里的**独立黄金参考 / UC 自有 verifier**，还是「harness 指纹 + 出处 attestation」就够？Agent 生成的 `correctness_harness.py` 是否仍算信任边界之内？

## Context

- F8 状态「部分」：剩余缺口是 harness 自报 `correctness`（`docs/agent-os-architecture.md`）。
- Oracle 已把 `BenchmarkResult.correctness` 当硬门；runner 在 `not correctness` 时短路。
- 用户 P0：「评测放到 Agent 改不到的环境里重跑，不信它的自报。」

## Options

1. **独立 verifier**：固定、只读的参考实现/测试在 protected 树外由 Gateway/runner 调用；Agent 工作区无写权限。
2. **Attestation only**：保留 harness 自报，但强制签名/指纹 + 禁止 Agent 改写 protected harness；接受 F8 残余风险。
3. **双闸**：attestation 过了仍要在隔离环境对 golden 再跑一遍才算 accepted。

## Proposed (from intake — needs human confirm)

➡️ **选项 3**：P0 至少做到「Oracle 接受前在 Agent 不可写路径重跑」；Agent 生成的 harness 不得单独关闭正确性门。完全自研 golden verifier 可分阶段，但开关默认要求独立重跑。

## Resolution

(fill in when decided)
