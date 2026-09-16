# T17 侦察笔记

## 1. 现状：无条件覆盖的确切位置

`crates/uc-engine/src/graph_store.rs`，`write_projection(&p, shadow, imported)` 的 shadow 分支：

```sql
INSERT INTO graph_nodes (graph_id, node_id, state, dependencies, required_capabilities, effect_class, type)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (node_id, graph_id) DO UPDATE SET
    state = EXCLUDED.state,          -- ← 问题所在
    dependencies = EXCLUDED.dependencies,
    required_capabilities = EXCLUDED.required_capabilities,
    effect_class = EXCLUDED.effect_class,
    type = EXCLUDED.type
```

该函数的 doc 注释**自陈**了这条语义：*"`shadow` switches the graph/node clauses from `DO NOTHING` (import: never clobbers a newer shadow write) to `DO UPDATE` (shadow: mirrors the authoritative HashMap)"*。⇒ 这不是疏忽，是**当时刻意**的设计（T2 时期镜像的定义是「镜像权威 HashMap」）；D15 裁决改的正是这个定义。

消费链：`upsert_task_shadow(&task)` → `project_task(task)` → `write_projection(..., shadow = true, ...)`。`upsert_task_shadow` 的调用方是 legacy `TaskStore` 的 fire-and-forget fan-out，快照在调用瞬间捕获 ⇒ 迟到即陈旧。

## 2. 守卫为什么**不能**放在 `project_task`

`project_task` 是**纯投影函数**：`Task → GraphProjection`，没有 DB 句柄、也不读 DB。它拿不到「该 node 现在是什么态」，**物理上无法判定转移**。⇒ 守卫只能落在写入点（`write_projection` / `upsert_task_shadow`）。

这不是偏好问题，票面「待裁 2」问的就是它。

## 3. `transition_ok` 的语义陷阱（本轮最重要的发现）

```rust
// graph_store.rs:227
pub fn transition_ok(from: &str, to: &str) -> bool { ... }
```

它**不是**「能不能写」的判据，而是「能不能**转移**」的判据。既有单测里有：

```rust
assert!(!transition_ok("READY", "READY"), "no self-loop");
assert!(transition_ok("RUNNING", "READY"), "fence re-arm edge");
```

⇒ **`READY → READY` 返回 false**。而镜像的**常规路径**恰恰是「快照状态与权威相同、只是来补 `type` / `dependencies`」—— 若直接拿 `transition_ok` 的布尔值当守卫，**常规路径会被整体误拒**，镜像就事实上废掉了（而它的失效外观是「什么都没发生」，与 T12 那次静默失效同型）。

**处置**：守卫条件取 `prev == incoming || transition_ok(prev, incoming)`，把「无转移」这一合法情形显式补上。`transition_ok` 仍是唯一的转移裁判。

## 4. 两个子句覆盖的是**方向相反**的两种陈旧快照（本节初版写反了，已由单测纠正）

**⚠️ 初版假设（错）**：我以为 `READY → RUNNING` 是普通派发的合法边，于是断言「#664 的形状会被 C 放行、只能靠 B 拦」。**单测第一次运行就把这个断言打红了** —— `transition_ok("READY", "RUNNING")` 返回 **false**。查既有单测确认：派发路径是 `READY → SCHEDULED → RUNNING`，`READY → RUNNING` 这条边**根本不存在**。

纠正后的矩阵：

| 权威态 | 迟到的快照 | `mirror_write_allowed` | 拦住回退的是谁 |
|---|---|---|---|
| `READY` | `RUNNING`（**#664 的形状**） | **false** ⇒ 拒绝 | **C** |
| `RUNNING` | `READY`（镜像形状） | **true** ⇒ 放行 | **B**（`state` 不在 SET 里） |
| `READY` | `READY` | true ⇒ 放行 | 无转移（本来就相同） |
| `RUNNING` | `SUCCEEDED` | true ⇒ 放行 | B |
| `SUCCEEDED` | `RUNNING`（复活） | false ⇒ 拒绝 | C |

⇒ **为什么 D15 必须 B+C**，现在有确切答案：两个子句各管一边。
- **C 单独不够**，因为「合法边」也能构成回退：`RUNNING` 权威 + 迟到的 `READY` 快照 —— `RUNNING → READY` **是**合法边（fence re-arm），C 会放行它。
- **B 单独不够**，因为它只保证「`state` 不被写」，而复活类形状（`SUCCEEDED` 权威 + 迟到 `RUNNING`）本该被**整体**拒绝（连 `type` / `dependencies` 都不该让陈旧快照碰），那是 C 的活。

两条合起来：**没有任何一类陈旧快照能移动 `state`，且非法边连触碰都不被允许。**

这一条已写进 `mirror_write_allowed` 的 doc（含表格）与单测（`assert!(!mirror_write_allowed("READY", "RUNNING"))` + `assert!(mirror_write_allowed("RUNNING", "READY"))` 各自显式钉住方向）。

**方法论留痕**：这个错误方向「看起来完全合理」（我甚至用「ordinary dispatch」为它背书），是**断言**而不是评审抓到的。若没有那条 `assert!`，错误的结论会同时进入代码注释、单测、本笔记与账本 —— 这是本仓第三次记录「先写断言再写结论」的价值（前两次是探针全 SKIP 与假绿）。

## 5. 可观测性设计（票面「待裁 3」的答复）

票面指出：**「镜像写入被正确拒绝」与「镜像根本没送达」的可见结果相同** —— 只看 DB 分不出来（两边 `state` 都一样）。

⇒ 需要**返回值层面**的可观测点。一次镜像触碰有三种结果：

| 结果 | `nodes` | `mirror_state_dropped` | `mirror_rejected` |
|---|---|---|---|
| 状态一致，补列（常规） | +1 | — | — |
| 状态不一致但合法转移（#664 形状） | +1 | **+1** | — |
| 非法转移，整体拒绝 | — | — | **+1** |

再加 `tracing::warn!`（带 `graph_id` / `node_id` / `authority_state` / `mirror_state`），让它在生产日志里也可见。#664 之所以难发现，正是因为**只靠 DB 观察**。

## 6. 已知限制 / 取舍

- **预取是一次 SELECT，不是逐节点查询**；它在 tx 内、且在 node 插入之前执行 ⇒ 读到的是「本事务之前的权威」，正是守卫要比较的对象。
- 同一 projection 内若出现**重复 `node_id`**，第二次判定仍以预取快照为准（不因第一次插入而更新）。`project_task` 从 subtasks 唯一映射，不会产生重复；即使产生，两次判定结果相同，无行为差异。
- **`graph_nodes` 的 `version` 列**仍不被镜像使用（票面提到过）。本票不改这一点：B 已经使 `state` 不再被镜像触碰，引入 CAS 属于另一层机制，且与「镜像只是补列」的新定位不匹配。
- 节点级 vs 逐步用量的口径问题（#666）与本票无关，`payload` 一行未动。
