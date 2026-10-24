# D4-approx-eval-enablement-gate

Status: **decided** (2026-10-24 — round-2 via「继续」adopting Proposed)
Type: grilling
Blocked by: D1 (resolved)
Blocks: P1 approx-eval implementation tickets

## Question

在启用秒级近似评测做快筛之前，必须满足怎样的**统计启用门**（与哪套「真实」评测的排序一致性、阈值、复测触发）？近似结果是否允许影响 Oracle 接受，还是只许排序/剪枝？

## Context

- 仓内无 DuoDiscover / rank-agreement 路径（assessment G1）。
- 用户 P1：先统计检验确认与真实评测排序一致，再快筛，最后 Oracle 确认。
- 风险：代码一改，近似与真实可能脱钩——需定期复测。
- D1：对外 uplift 只用 report（hold-out）；rank agreement 的真值集应对齐 report 角色。

## Resolution

**剪枝-only + 显式启用门。**

1. 近似评测**只淘汰明显差的候选**；接受门与报告数字永远来自全量 Oracle（受 D1 约束的 select / report 集）。
2. 功能开关仅在 Spearman/Kendall（实现票锁定其一或二者）于固定 hold-out 真值集（**report** 角色，per D1）上通过排序一致性阈值后才允许开启。
3. 任何时候**报告 uplift 不得引用近似分**。
4. 代码或 bench 变更强制复测；失败则**自动关闭**该功能开关。
