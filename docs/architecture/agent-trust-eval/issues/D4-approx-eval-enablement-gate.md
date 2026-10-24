# D4-approx-eval-enablement-gate

Status: open (decision ticket — not implementation)
Type: grilling
Blocked by: D1
Blocks: P1 approx-eval implementation tickets

## Question

在启用秒级近似评测做快筛之前，必须满足怎样的**统计启用门**（与哪套「真实」评测的排序一致性、阈值、复测触发）？近似结果是否允许影响 Oracle 接受，还是只许排序/剪枝？

## Context

- 仓内无 DuoDiscover / rank-agreement 路径（assessment G1）。
- 用户 P1：先统计检验确认与真实评测排序一致，再快筛，最后 Oracle 确认。
- 风险：代码一改，近似与真实可能脱钩——需定期复测。

## Options

1. **剪枝-only**：近似评测只淘汰明显差的候选；接受与报告数字永远来自全量 Oracle（受 D1 约束的那一套）。
2. **门控接受**：近似过线才允许进全量；近似本身永不写 uplift。
3. **弱 Oracle**：在 rank agreement 持续达标时可暂代 Oracle（高风险，需极强门）。

## Proposed (from intake — needs human confirm)

➡️ **选项 1 + 显式启用门**：Spearman/Kendall（具体选哪个在实现票锁定）在固定 hold-out 真值集上过阈值后，功能开关才允许近似快筛；任何时候报告 uplift 不得引用近似分。代码或 bench 变更触发强制复测，失败则自动关开关。

## Resolution

(fill in when decided)
