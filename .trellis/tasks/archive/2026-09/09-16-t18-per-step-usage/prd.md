# T18 — 逐步用量落到 `payload.steps[]`

- **Issue**: #668（实现票）
- **Implements**: #666（**已裁 2026-09-16 — 裁决 C：只走 `payload`**）
- **Map**: P2 wayfinder map #656
- **Related**: T15 #660（上报契约 + `cost`/`tokens` 落列）、D13 #657（指标采集点）、T14 #659（写入点 + `duration_ms`）、T16 #661（同一集结口）

## 要解决的问题（一句话）

`Worker._execute_steps` 的**每一条**返回路径都只透传**一步**的 usage，而终态事件仍声明 `usage_reported: true`
⇒ 下游把一个「多步 workflow 的最后一步的量」当成「该节点的完整用量」，**系统性偏低且看不出来偏低**。
D13 的覆盖率机制只能发现「完全没上报」的节点，**发现不了这种失真**。

## 一手事实（2026-09-16 实测，非票面转述）

| 事实 | 来源（命令/文件） |
|---|---|
| `_execute_steps` 的 4 条 return 各只透传一步 | `worker.py:1366-1373`（步骤失败 abort）、`1468-1475`（并行组失败）、`1482-1486`（无步骤）、`1493-1500`（正常结束） |
| 「不累积」是刻意选择：`all_file_changes` **累积**，`token_usage` **不累积** | `worker.py:1496` + `1487-1492` 的自陈注释 |
| `_make_task_update_payload` 是**唯一**的 subtask 条目集结口 ⇒ 在此发 `steps` 可覆盖全部三个 publisher | `nats_worker.py:155-166` 的 T15/T16 论证 |
| `subtask.steps` 的两种形状都真实存在：简单 subtask **省略** steps（单 agent），中等复杂发 **3 步链** | `packages/uc-orchestrator/src/orchestrator/orchestrator.ts:2093-2099`（decomposer 指令原文） |
| 适配器**可以**不报 usage ⇒ 「某步 `usage: null`」是可达的真实情形 | `sandbox.py:1500`（`token_usage` 初值 `None`，仅当流里有 usage 事件才赋值）；`sandbox.py:1307` 是唯一构造点 |
| 终态 payload 由 **Rust** 写（Python 只管发 `uc.task.update`）⇒ 必须贯通 Rust 侧才能落到 `execution_events.payload` | `graph_store.rs:1876-1897`（`node_succeeded` 唯一写入点，在 `commit_once` 事务内） |
| `NatsSubtaskUpdate` 有 **29** 处结构体字面量，**全部**在 `#[cfg(test)]`（module 起于 `server.rs:5352`）且形状统一（`usage: None,` 独占一行） | `grep -c "^ *usage: None,$" crates/uc-grpc/src/server.rs` = 29；`grep -c "NatsSubtaskUpdate {"` = 30（含定义行） |
| `commit_once` 有 **15** 处调用（1 真 + 14 测试） | `grep -rn "\.commit_once(" crates/` |
| `on_commit` 有 4 处（trait 默认 / storage impl / uc-grpc 测试 fake / 测试调用） | `graph_store.rs:684,2977,3310`、`server.rs:8637` |
| `usage_reported` / `usage_source` **没有** TS 消费者 | Grep `usage_reported\|usage_source` 于 `packages/` = 0 命中 |

## Scope

1. `_execute_steps` 收集**每步**的用量与来源，随返回值上浮。
2. 每步至少含 `step_index` / `parallel_group` / `usage`（同 `SubtaskUsage` 形状）/ `source`（适配器名，缺失则 `null`）。
3. `usage` **允许 `null`** ——「不带数字就不编造」，与 D13「不得当 0」同源。
4. **并行组逐条明细**：组内成员各自一条，**不做组级合计**。
5. `contract_version` **不动**（加性）。

## 设计决定（本票新增，逐条给理由）

### D1 —— 「一条 = 一个**已执行**的单元」，跳过的步**不留条目**

`step_index` = 该单元在 subtask **执行序列**中的位置：workflow 形态下等于它在 `subtask.steps` 里的下标；
legacy 单 agent 形态下恒为 `0`（那一次执行本身就是该 subtask 的唯一单元）。

条件为假而被跳过的步**不产生条目** ⇒ `step_index` 会出现**跳号**，而这个跳号**就是**「那一步没执行」的编码。

> 为什么不给跳过的步发一条 `usage: null`？因为那样一来 `usage: null` 就有两个含义
> （「跳过了」与「跑了但适配器没报」），而验收 2 要的恰恰是后者可读。**两种事实必须可分**。

### D2 —— `source` = `usage.source` → 回落到 `step.agent` → `null`

T15 的 `usage.source` 是**实测来源**（适配器在解析点自己盖的章），故优先；
适配器没报数字时 `usage` 为 `null`，此时回落到该步**声明的**适配器名（`step.agent`）；
两者都不知道才是 `null`（票面「缺失则 `null`」）。
这与验收 1「`source` 与各步**实际上报**一致」直接对应。

### D3 —— 步条目的 `usage` 用**显式 `null`**，而 subtask 条目沿用 T15 的**省略键**

两处纪律不同，是**刻意**的：

- subtask 条目的 `usage` 是 T15 的**线格式增量**（`SubtaskUsage` 各字段 `skip_serializing_if`，整块 `None` 时省略键）——
  既有消费者与 Rust `#[serde(default)]` 都建立在这个形状上。
- `payload.steps[]` 是**位置记录**（数组元素按 `step_index` 对齐），显式 `null` 让
  `steps[i].usage` 恒可索引，不必先判键是否存在。

`payload` 是**只写一次**的 JSON 文档，不会被反序列化回领域类型 ⇒ 没有往返风险。

### D4 —— legacy（`subtask.steps` 为空）也要有一条

验收 5 明确要求单步 subtask 的 `steps[]` 长度 1，而 decomposer 对简单 subtask 是**省略 steps 的**
（见上表）⇒ 多数 subtask 走的正是 legacy 路径。若不给它发条目，`steps` 键会在多数 subtask 上缺席，
与验收 5 的兼容意图相悖。故：legacy 路径发**一条** `step_index: 0` / `parallel_group: ""` 的记录。

⚠️ 注意 `payload.steps[]`（**用量记录**）与 `uc.subtask.execute` 里的 `steps`（**步骤定义**）
是**同名不同物**：前者是「跑了什么、花了多少」，后者是「编排声明了什么」。本票只动前者。

### D5 —— 节点级三列与 `usage_reported` **一律不动**

`payload.steps[]` 是**拆解视图**，不是新的口径：`cost` / `tokens` / `duration_ms` 仍是该 node 终态事件的用量
（**不是**各步之和），`usage_reported` 仍只回答「该节点有没有报过数」。
改为求和会让历史数据与新增数据不可比，且要重新论证 fence 语义 —— 票面已列为 out of scope。

## Acceptance

1. **多步串不同适配器**（≥2 步）跑完 ⇒ 终态事件 `payload.steps[]` 长度 == 执行单元数，每项 `step_index` 严格递增、
   `parallel_group` 与编排一致、`usage` / `source` 与各步实际上报一致。
2. **某步没上报** ⇒ 该项 `usage: null`，其余步正常带值。
3. **并行组逐条明细**：并行组的 N 个成员产生 N 条 `steps[]`，**无组级合计**。
4. **节点级三列语义不变**：`cost` / `tokens` / `duration_ms` 仍未改口径（`usage_reported` 亦不变）。
5. **单步 subtask 的 payload 保持可读**：`steps[]` 长度 1（含 legacy 单 agent 路径），不破坏既有消费者。
6. `contract_version` 未 bump；**不新增列、不新增事件类型（零迁移）**。
7. 门禁全绿，Python 与 Rust 基线只增不减。

## Out of scope

- 「节点级三列是否改为各步之和」—— 独立问题，需另开决策票。
- 「一次 commit 恰好一行事件」这条性质**不动** —— 这正是选 C 而非 A（N 步 → N 行）/ B（新事件类型）的理由。
- review / 其他事件的 payload 形状。

## 边界（已知且刻意保留）

- **单步与多步在 `steps[0]` 上的来源不同**：workflow 形态取 `subtask.steps[0]`，legacy 形态取那一次隐式执行。
  两者都是「该 subtask 的第一个执行单元」，语义一致；但**没有** `subtask.steps[0]` 与之对应
  （legacy 形态下 `subtask.steps` 为空）。已在 D4 说明。
- **跳过的步不产生条目**（D1）⇒ `steps[]` 长度 < `len(subtask.steps)` 是**正常**的，不是丢数据。
