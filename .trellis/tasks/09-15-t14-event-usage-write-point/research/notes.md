# T14 研究笔记（#659）

## 1. 为什么是「写者缺席」而不是「schema 缺失」

- 建表 DDL（`graph_store.rs:795-810`）：`cost NUMERIC(18,6)` / `tokens BIGINT` /
  `duration_ms BIGINT`，自 T2（#638）就在。
- DDL 上方注释（`:793-795`）：*"the reserved billing columns that close the
  assessment's leftover-risk. No writer exists in T2 … the schema lands now so T3 never
  needs a widening migration."* ⇒ 作者当时就把它标成**预留**，等的是写者，不是等 schema。
- 唯一 INSERT（`append_event_tx`）只绑六列；其 doc 注释写着 *"deliberately never bound"*。
- 读方盘点：`grep execution_events` 全仓 —— Rust 侧只有建表与 INSERT/HISTORY 查询用
  `event_type` / `payload`；**Python 侧完全不引用 `execution_events`**；
  `SELECT` 里从未出现 `cost` / `tokens` / `duration_ms` ⇒ 三列是死列，不是"读了但没值"。

## 2. 时长为什么能立刻拿到

`commit_once` 的成功路径（`graph_store.rs:1662-1672`）在同一事务里做：

```sql
UPDATE task_attempts SET status = 'SUCCEEDED', finished_at = NOW(), …
WHERE attempt_id = $1 AND status = 'RUNNING'
```

而 `started_at` 由 `schedule_attempt` 写入。两个时间戳都在同一个事务里可读 ⇒
无需新数据源、无需契约变更。**用 `SELECT … NOW()` 取数据库时间**，不用进程时钟：
应用与 PG 的时钟差会把时长污染成看似合理但错的数。

## 3. 为什么写点必须是 commit_onсe 而不是别处

- 终态只有一条：commit-once 保证 `(graph, node)` 只有一个 winner。
- fenced / 迟到的 result 在 `fenced` 早退与 `res.rows_affected() == 0` 两条路径上
  **不进入成功分支** ⇒ 它们的 `late_result` 事件天然不带 `duration_ms`。
- 结论：把 usage 绑在 `node_succeeded` 上 ⇒ 用量 **exactly-once**，重试不会双计。
  这条不是"顺便得到"，而是本票设计上依赖的性质，所以要被测试钉住。

## 4. 三条判据（纯函数）与它们的理由

| 输入 | 输出 | 理由 |
|---|---|---|
| `started_at = None` | `None` | 没启动过的 attempt 没有时长；写 0 就是**编**一个数 |
| `db_now < started_at`（时钟倒挂） | `None` | 取绝对值会把倒挂伪装成一个合理的正数 |
| 正常 | `Some(ms)` | 含两时间戳相同 → `Some(0)`，那是**真实的 0** |

⇒ `0` 与 `NULL` 不可混同：前者是"量到了，值就是零"，后者是"量不到"。
下游读者据此区分"无用量上报"与"零用量"。

## 5. 绑定类型的一个真实坑

`cost` 是 `NUMERIC(18,6)`，而 sqlx 把 Rust `f64` 当作 `FLOAT8`：
- **写**：显式写成 `$7::numeric`，把 float8→numeric 的转换变成**写下来的决定**，
  而不是依赖 PG 的隐式赋值 cast。今天 `cost` 恒为 NULL，但 T15 绑定真值时会立刻踩到。
- **读**：`f64` 的 sqlx `Type<Postgres>` 是 `FLOAT8`，而 `Type::compatible` 默认按
  类型相等判定 ⇒ **把 NUMERIC 列解码成 `Option<f64>` 会在运行时直接报类型不匹配**。
  所以集成测试读的是 `cost IS NULL`（`bool`），而不是 `cost`（值）——测试要问的是
  "写者有没有碰这一列"，不是"里面是什么数"。

## 6. 门控

`attempt_duration_ms` 只被 storage-gated 的 `commit_once` 调用 ⇒ 与 `EventUsage` /
`append_event_with_usage_tx` 一样加 `#[cfg(feature = "storage")]`，否则
`cargo test -p uc-engine --no-default-features` 下是 dead_code。对应地，
两条单测也带同一门控。

## 7. 集成测试的真跑与假通过判据

- 命令（本地）：`UC_PG_URL_TEST=postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders`
  `cargo test -p uc-engine --features storage --test graph_store_integration -- --ignored --nocapture --test-threads=1`
- **必须带 `--nocapture`**：`connect_or_skip` 的 `SKIP` 走被 libtest 捕获的输出流，
  不带时日志里数不到 SKIP，于是 `0 skipped` 既可能是真跑也可能是全跳过。
- **看耗时**：真跑 1–4s；假通过恰好 30s/60s（sqlx acquire_timeout × 用例数）。
- 用例名不含 `postgres` ⇒ 不进 CI 的 `postgres integration tests`（那 job 的末位
  `postgres` 是**测试名过滤器**），归 `storage integration tests`（workspace 级
  `cargo test --features storage -- --ignored`）。
