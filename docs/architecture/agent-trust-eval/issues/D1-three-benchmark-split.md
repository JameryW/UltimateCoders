# D1-three-benchmark-split

Status: open (decision ticket — not implementation)
Type: grilling
Blocked by: _(none — frontier)_
Blocks: D4; P0 implementation tickets for Oracle / MetaInfer uplift reporting

## Question

搜索候选、挑选接受、报告最终 uplift，是否必须使用**互不相交**的三套 benchmark？各自的权威所有者是谁，Agent / MetaInfer / UC Oracle 分别允许看见哪一套？

## Context

- 现状：同一 `BenchmarkRunner` / 固定 workload 既服务迭代反馈又服务最终 `improvement_pct`（见 assessment G6）。
- 风险类比：Terminal-RSI 式选择偏差——在用来挑赢家的集合上报告提升。
- 用户 P0：「把用来搜索、用来挑选、用来报告最终结果的三套 benchmark 分开。」

## Options (non-exhaustive)

1. **严格三分**：search / select / report 三套互斥；报告 uplift 只能引用 report 集；search 集可被 Agent 看见。
2. **两分**：search+select 共用开发集，report 为 hold-out；Oracle 接受门用 select，对外数字只用 report。
3. **同集 + 披露**：暂不分裂，但所有对外数字标注 `selection-biased` 且禁止进 README 无标记数字。

## Proposed (from intake — needs human confirm)

➡️ **选项 2 作为 P0 MVP，选项 1 为 P1 目标形态**：先强制 hold-out report 集与「挑选/接受」集分离；search 与 select 可暂共用但必须在产物里标记 `eval_role`。对外/README 数字只能来自 report 集。

## Resolution

(fill in when decided)
