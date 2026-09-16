# T16: review 作为图上节点（D14 #658 已决）

- **票**：#661 ｜ **地图**：#656（P2-2 Blackboard review）｜ **决策**：D14 #658（已裁并关闭）
- **日期**：2026-09-16

## 背景

T6（#642）删除了整条 TS review 流水线（`reviewSubtask*` / `parseReviewOutput` / `WORKER_PROMPT`
/ `SUPERVISOR_PROMPT`），并在 `orchestrator.ts:1783-1790` 留下前向口径：
"review semantics will be re-established by the Rust **ready-node pipeline**"。
D14 据此裁决：**review 是图上的节点**，靠既有能力门路由、既有依赖边排序，不引入新机制。

本票做的是把这个裁决落成**可运行、可断言**的通路。

## 本票必须先裁的未决项（票面留给本票）

**review 节点由谁插入 —— 选 (b)「上游显式声明依赖」。**

| 选项 | 取舍 |
|---|---|
| (a) 分解时按策略插入 | 表达力强，但要改分解路径（Python `sandbox.py` 分解 + Rust 侧 DAG 生成），等于引入"策略"这一新机制 |
| **(b) 上游显式声明依赖** ✅ | **零新机制**：谁要 review，就在图上写一个 review 节点 + 依赖边。与 D14「不引入任何新机制」的约束一致 |

**mark 的约定**：一个节点是 review 节点 ⟺ 它的 `required_capabilities` 含 `"review"`。
选这个标记而不是新增声明字段，理由：

- `required_capabilities` 在 Rust `Subtask`、`SubtaskProto`、TS `TsSubtask`/`SubtaskDef`
  **两侧四种形态里都已存在** ⇒ 用它能让 (b) 的"写个 review 节点"在**零新字段**下可表达；
- `type` 列按 D14 是**只写不读、仅限标注** ⇒ 从能力派生它，等于让标注只有一个真源，
  不会出现"type 写了 review 但能力没带"的分叉。

## Scope（与票面对齐）

1. **S1 图平面**：`graph_nodes.type` 落写者。`NodeRow` 加 `node_type`，两条投影路径
   （`project_task` / `project_ts_task`）+ 唯一 INSERT 站点写 `type` 列。
2. **S2 能力路由**：review 节点 `required_capabilities: ["review"]`，走既有
   `dispatch_gate` ⇒ **不改派发路径**。
3. **S3 独立性**：把 `"review"` 从 **worker 默认能力清单**里摘掉 ⇒ 产出方 worker 通不过
   review 节点的能力门。靠**能力门**表达，不改派发代码。
4. **S4 内容通路**：review 结论经 `SubtaskResult.review` 送达 TS `SubtaskDef.review`
   （该字段 T6 刻意保留）⇒ UI 无需改动。
5. **S5 兼容**：全程加性，不 bump contract_version；老端忽略未知键。

## 验收（票面原文）

- 一个 review 节点被正确地按依赖门控调度（上游未完成 → 不被派发；上游完成 → 可派发），状态由既有统一规则产出。
- review 只被**非产出方**的 worker 认领（同一 epoch 自审被拒）。
- review 结论经 `SubtaskResult.review` 送达，UI 无需改动即可显示。
- 不新增表、不新增列、不改派发/readiness 路径（若发现必须改，说明 D14 的模型有洞 —— 回来重开 D14，而不是就地扩权）。
- 门禁全绿且 Rust 基线只增不减。

## 非目标

- review 的**策略**（审什么、审几轮、失败怎么重试、verdict 如何判定）—— 本票只做通路。
- 指标采集（T14/T15 已完成）。
- UI 渲染（T6 保留的字段已能渲染）。

## 开工前对票面的三处更正（实证，见 research/notes.md §1）

1. **D14 说 `SubtaskResult.review` 被刻意保留 —— 只对 TS 成立。** Rust `SubtaskResult`
   （`uc-types/src/agent.rs:210-232`）与 Python `SubtaskResult`（`types.py:153-174`）
   **都没有** `review` 字段。UI 能渲染是因为 **TS** `SubtaskDef.review`
   （`orchestrator.ts:110`）还在。⇒ S4 要补的是 **Rust/Python → proto → TS 桥**这条链。
2. **`graph_nodes.type` 有列但无写者。** 唯一 INSERT 站点（`graph_store.rs:1030/1038`）
   的列清单里**没有 `type`** ⇒ 全走 DEFAULT `'subtask'`。D14 的"零迁移"成立，
   但"零工作"不成立 —— 本票必须提供写者。
3. **`worker_epoch` 不是"worker 身份 epoch"。** 它是 per-attempt 的单调栅栏
   （`max(epoch)+1`，`graph_store.rs:1411`），Python 侧恒发 `""`。
   ⇒ 票面"同一 epoch 自审"实际指**同一 worker 不得自审**，本票按此实现。
