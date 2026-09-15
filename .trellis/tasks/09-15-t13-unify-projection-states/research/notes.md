# T13 侦察笔记（#655）

## 1. 三条投影路径与各自的 Pending 判定

| 路径 | 入口 | Pending 判定 | 结果 |
|---|---|---|---|
| Source A（PG tasks JSONB，Rust serde 形） | `project_task`（`graph_store.rs:310`）→ `node_status_of_subtask`（L159） | 依赖感知 | `CREATED` ✅ |
| Shadow（活 Task HashMap） | `project_task` | 依赖感知 | `CREATED` ✅ |
| **Source B（`.uc/tasks/*.json`，camelCase）** | **`project_ts_task`（L415）→ `node_state_token`（L117）** | **纯 token 映射** | **`READY` ❌** |

`node_state_token` L119：`"pending" | "ready" => "READY"` —— 歧义 token 的歧义就在这一行。

## 2. 同文件内的两条矛盾表述

- **L43-55 模块头映射表**：标题写「(import + shadow, single source of truth below)」，表里 `Pending` / `pending` → `READY`。
  → 该表对 shadow 的描述**从写下那天起就是错的**（shadow 走 `project_task`，即依赖感知）。所以这张表不能当作"设计意图"的证据，它本身是失效文档。
- **L145-158 `node_status_of_subtask` 文档注释**：明确说把每个 `Pending` 映成 `READY` 是**它存在要阻止的 bug**（"publishes the whole task as schedulable — including nodes whose dependencies have not run"），且点出投影无条件写 state 会覆盖图自己的依赖派生状态。

## 3. git 历史：不是"改了没同步"，是同源非对称

```
$ git log -S "fn node_status_of_subtask" -- crates/uc-engine/src/graph_store.rs
28b66e9 feat(engine): five graph row tables + idempotent dual-source backfill
$ git log -S "| \`Pending\` / \`pending\` | \`READY\` |" -- crates/uc-engine/src/graph_store.rs
28b66e9 ...
$ git log -L 426,426:...   # project_ts_task 里的 node_state_token 调用
28b66e9 ...
```

三处**同属一个提交**（T2 #638）。提交正文（`git log -1 28b66e9`）未提及这条非对称的任何理由，也没有任何注释声明它是刻意的。

## 4. TS 词表：`pending` 的歧义在 TS 侧同样存在

`packages/uc-orchestrator/src/orchestrator/orchestrator.ts`
- L100：subtask 状态联合 `"pending" | "running" | "reviewing" | "completed" | "failed" | "cancelled"`。
- L1807-1810 `toPersisted`：`status: s.status` **原样**写盘，不做可运行性解析。
- L554 / L644 / L1128：subtask 的**初始**状态即 `"pending"`。
- 仓库自己的 fixture：`scheduler.test.ts:119` `{status:"pending", dependsOn:["st-1"]}`、`progress-widget.selfcheck.ts:532` `{dependsOn:["missing"]}`。

⇒ 一个"已提交未开始"的任务从 `.uc/tasks` 迁移进来时，**每个阻塞节点都会写成 `READY`**。这不是边角情形，是默认状态。

## 5. 爆炸半径（诚实口径：不是活 bug）

- **今天无生产消费者**：`GraphStore::node_state`（L2109）的调用者全是测试（`granular_cancel_e2e.rs`、`pause_grace_diamond.rs`）。所以不是线上活故障。
- 害处是**给 durable 平面播种错值**：模块头 L4-7 写明"the gateway's in-memory HashMap stays the runtime authority"，而图平面正被做成权威（T3 起有 CAS 写动词）→ 错种子会被迁移继承。
- **边界**（也是它至今无人察觉的原因）：
  - source-B 导入需显式设 `UC_GRAPH_IMPORT_DIR`（L34-38）；
  - `graph_exists` 命中即 skip（L1160）；
  - 写路径 `write_projection(&p, shadow=false, imported=true)` → node SQL `ON CONFLICT DO NOTHING`（L972-976）⇒ **只能插不能覆盖**；
  - 若该任务随后在 Rust 侧活起来，`upsert_task_shadow`（`DO UPDATE SET state = EXCLUDED.state`，L967-971）会用正确值覆盖 → **错值 + 延迟自愈**，不同于 T12 那种"功能整体死掉"。

## 6. 为什么现有测试测不出（与 T12 同形）

- `project_ts_task` 单测（L3098-3141）**注释自己声明了不变量**："The same logical graph expressed as TS PersistedTask JSON must produce identical node ids/**states**" —— 而 fixture 状态只有 `completed` / `assigned`，**恰好绕开唯一有歧义的 `pending`**。
- Source A 集成测试（`graph_store_integration.rs:463-503`）有 Pending 节点 `n4`，但其 `depends_on` 是**空集** ⇒ 空依赖集天然满足 ⇒ 两种实现都给 `READY`，同样不区分。
- 净结果：依赖感知规则的 `CREATED` 分支**只在 Rust 路径上被测**（L3073 `"unmet dependency is not runnable"`），TS 路径零覆盖。

## 7. 修复设计

抽一个共用规则函数，判据用 **"依赖的 status token 是否 `SUCCEEDED`"**（词汇无关，避免再写一份状态匹配表 → 不会再漂移）：

```rust
fn dependency_aware_state(
    raw_status: &str,                       // 交给 node_state_token
    deps: &[String],                        // 该节点的依赖 id
    satisfied: &dyn Fn(&str) -> bool,       // 依赖 id → 是否已完成
) -> String {
    let state = node_state_token(raw_status);
    if state != "READY" { return state; }
    if deps.iter().all(|d| satisfied(d)) { "READY" } else { "CREATED" }
}
```

- `node_status_of_subtask` 改为调用它（把 `SubtaskStatus` 索引适配成谓词）。
- `project_ts_task` 建立 `HashMap<&str, &str>`（id → status）并传入同一谓词。
- 判"已完成"统一为 `node_state_token(dep_status) == "SUCCEEDED"`（TS 的 `completed` 与 Rust 的 `Completed` 都映到它）。
