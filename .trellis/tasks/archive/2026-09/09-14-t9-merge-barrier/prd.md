# T9: Merge barrier — IssueMergeGrant/ReportMergeOutcome + merge_grants 表 + arbiter 接入 (#651)

## 背景

D5 #634 + D9 #646：commit barrier 归属 hybrid——Python MergeArbiter 保留执行
（git effects），Rust 网关签发 fenced 单写者 merge 授权。本票做垂直切片。

## Scope

1. **Proto**（TaskService）：
   - `IssueMergeGrant(IssueMergeGrantRequest{graph_id}) -> {granted, merge_idempotency_key, idempotent_replay, error}`
   - `ReportMergeOutcome(ReportMergeOutcomeRequest{graph_id, merge_idempotency_key, status, merged_branches, conflict_branches, push_status}) -> {accepted, idempotent_replay}`
2. **Migration**：`merge_grants`（graph_id PK, merge_idempotency_key, issued_at,
   consumed_at, outcome JSONB）——graph_id 主键 = 每 graph 单条当前授权，被取代的
   旧 key 自然变 unknown（accepted=false）。
3. **Rust gate**：签发以图静止为闸（graph_nodes 全 terminal）；canonical key
   `sha256("merge:{graph_id}:{SUCCEEDED ids CSV}:{node_id=sha256(output) pairs ';'}")[:32]`
   （节点按 node_id 字典序；output = node_completions.result_ref，空补 ""）；
   report 落 consumed_at + outcome；同 key 已消费重放 → `accepted=true,
   idempotent_replay=true`；unknown/superseded key → `accepted=false`。
   Issue 同 key 重放：consumed → granted=true+replay=true（arbiter 跳过合并）；
   未 consumed → granted=true+replay=false（照常合并，崩溃恢复路径）。
4. **Python arbiter**：`_arbitrate_task` 合并前取授权（opt-in `merge_gate`，
   由 nats_worker 在 gRPC endpoint 配置时接线）；`granted=false` → log+skip
   （绝不未授权合并）；`replay=true` → 跳过合并 + no-op report；拿到 key 后
   先 arbitrate 再携 key report（report 非 fatal）。gate 未配置 → 维持现状
   （legacy 无网关部署）。
5. **跨语言 golden**：preimage `merge:g-1:n-1,n-2:n-1=<sha(out-1)>;n-2=<sha(out-2)>`
   的 hex[:32] 钉死在 Rust + pytest 两侧。

## 验收（票面）

- 非静止图 → granted=false（Rust 测）。
- 已消费 key 重放 → no-op + idempotent_replay=true（Rust+Python 测）。
- 陈旧聚合（不同 SUCCEEDED 集）→ 不同 key → accepted=false。
- 跨语言 golden：同图态两侧派生同 key。

## 非目标

- TS/Dashboard 消费 verbs（proto 生成物 Rust 侧 build.rs 自动；TS 不消费）。
- merge_grants 历史（多行/审计）——graph_id PK 单行即可（superseded 即失效）。
