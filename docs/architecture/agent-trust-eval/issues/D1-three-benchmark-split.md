# D1-three-benchmark-split

Status: **decided** (2026-10-24 — intake round-1 via「继续」adopting Proposed)
Type: grilling
Blocked by: _(none)_
Blocks: D4; P0 implementation tickets for Oracle / MetaInfer uplift reporting

## Question

搜索候选、挑选接受、报告最终 uplift，是否必须使用**互不相交**的三套 benchmark？各自的权威所有者是谁，Agent / MetaInfer / UC Oracle 分别允许看见哪一套？

## Context

- 现状：同一 `BenchmarkRunner` / 固定 workload 既服务迭代反馈又服务最终 `improvement_pct`（见 assessment G6）。
- 风险类比：Terminal-RSI 式选择偏差——在用来挑赢家的集合上报告提升。
- 用户 P0：「把用来搜索、用来挑选、用来报告最终结果的三套 benchmark 分开。」

## Resolution

**P0 = 两分；P1 目标 = 严格三分。**

1. **Report（hold-out）** 与 **Select（接受门）** 必须互斥。对外数字、README、发布产物里的 uplift **只能**引用 report 集。
2. **Search** 与 **Select** 在 P0 可暂共用同一开发集，但产物必须带 `eval_role`（`search` | `select` | `report`），禁止把 search/select 分数写成 report。
3. UC Oracle 的接受门使用 **select**；报告/对外 uplift 使用 **report**（另一次受保护重跑）。MetaInfer 内部 ranking 不得直接写入对外 uplift。
4. P1 再把 search 从 select 中拆出，形成严格三分。
