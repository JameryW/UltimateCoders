# T14: 指标写入点 + duration_ms —— 终态事件带时长（#659）

> P2 wayfinder 地图 #656 ｜ 决策 D13 #657 ｜ 阻塞 T15（usage 上报契约）

## 背景

`execution_events` 的 `cost NUMERIC(18,6)` / `tokens BIGINT` / `duration_ms BIGINT`
三列自 T2（#638，提交 `28b66e9`）建表就存在（`graph_store.rs:806-808`），
但唯一的 INSERT 只写六列，且 `append_event_tx` 的注释明确写着它们是
*"deliberately never bound — they stay NULL for their reserved writers"*。
全仓（含 Python 侧）**没有任何读方** ⇒ 三列今天是死列。

D13（#657）查明：缺的不是 schema，而是**写者**。本票先写能立刻拿到的那个维度——**时长**。

## 目标

让**成功提交的终态事件**（`node_succeeded`）带上 `duration_ms`，取值为该 attempt 的
真实运行时长；`cost` / `tokens` 保持 NULL，等 T15 的契约片。

## 为什么 duration 先做

- **不需要新数据源**：`task_attempts` 已有 `started_at`，而同一事务里就会
  `UPDATE task_attempts SET status='SUCCEEDED', finished_at = NOW()`（`graph_store.rs:1664`）
  ⇒ 两个时间戳都在手边。
- **零契约变更**：不碰 `SubtaskResult`、不碰信封 ⇒ 不涉及 lockstep，不影响任何 worker。
- 写点与 commit-once 天然重合 ⇒ 时长天然 exactly-once。

## 范围

**在范围内**

1. 纯函数 `attempt_duration_ms(started_at: Option<DateTime<Utc>>, db_now: DateTime<Utc>) -> Option<i64>`：
   - `started_at` 为 `None` → `None`（没有起点就不编一个）
   - 负值（时钟倒挂）→ `None`，**不取绝对值**
   - 正常 → 毫秒（两时间戳相同是**真实的 0**，不是缺失值）
2. `commit_once` 的 attempt `FOR UPDATE` 查询同时取 `started_at` 与 DB 的 `NOW()`，
   用该函数算 duration。**用 DB 的 `NOW()`，不用进程时钟**，避免应用与 PG 的时钟差。
3. `append_event_tx` 拆成「不绑用量的薄封装」+ `append_event_with_usage_tx`（绑三列）；
   只为成功 commit 的 `node_succeeded` 传 `Some(usage)`，其余 11 个调用点行为不变。
4. 那条 "deliberately never bound" 的注释改写成：**duration 已由本票写入；
   cost/tokens 仍为 T15 保留**。
5. `node_succeeded` 的 payload 加 `"usage_reported": false` —— 显式表达"本次没有用量上报"，
   **避免把缺失误读成 0**（D13 的硬性要求）。

**不在范围内**

- `SubtaskResult` 的 usage 字段与 worker 侧填充（T15 #660）。
- 指标本身的定义与计算（Useful Work Ratio 等）—— 本票只负责让数据存在。
- 任何读方：本票只写不读，读方随 T15/后续票落地。

## 验收

- [ ] 成功 commit 后，该 attempt 的 `node_succeeded` 事件 `duration_ms` 非空，且等于
      `finished_at - started_at`（容差内）
- [ ] **fenced / 迟到结果不写** `duration_ms`（`late_result` 事件不带用量）——写点与
      commit-once 重合的天然结果，需被测试钉住，且重试不得追加第二条终态事件
- [ ] `started_at` 为 NULL 的 attempt → `duration_ms` 为 NULL，**不是 0**
- [ ] `cost` / `tokens` 保持 NULL（T15 才写），**不零填充**
- [ ] 纯函数单测（本地可跑）+ 集成测试（`graph_store_integration.rs`，PG，
      由 CI 的 `storage integration tests` job 实跑）
- [ ] 门禁：`fmt` / `clippy -p uc-engine --all-targets -D warnings` / 默认模式 lib 测试**只增不减**

## 实现期的一处命名修正（诚实记录）

issue 正文写的函数名是 `node_duration_ms`，实现落为 **`attempt_duration_ms`**。
理由：函数量的是**某个 attempt** 的时长（`started_at`/`NOW()` 都取自 `task_attempts` 行），
一个 node 可以有多个 attempt；叫 `node_*` 会掩盖这个区别，也与既有的 `attempt_id` /
`task_attempts` 词汇不一致。行为与 issue 描述的三条判据**完全一致**。

## 风险

**低。** 写的是今天全仓无人读的三列之一；`late_result` 路径不写；`commit_once` 的
原有 CAS / commit-once / fence 语义一行未改（只把 attempt 的 `SELECT status` 扩成
`SELECT status, started_at, NOW()`，`FOR UPDATE` 与 `_ => true` 的兜底分支保持不变）。
