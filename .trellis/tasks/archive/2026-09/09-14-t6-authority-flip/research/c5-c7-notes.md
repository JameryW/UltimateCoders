# T6 #642 C5/C6/C7 同日交付记录（2026-09-14）

## C5 — conflict_risk 分级（e4cb8f6）

- scheduler.ts 新增 `fileOverlapRatio`（共享文件数/较小归一集，min 归一对称）与
  `classifyConflicts`（最坏两两重叠：>=0.4 medium / >=0.8 high，空集安全），复用 F47
  normalizeFileIntent。字段随 SubtaskDef→SubtaskResult→PersistedTask 缓存流转，不进
  proto/graph（P1 affinity 留接口）。
- 消费点 reconcileTask 认领段：约束作用于**本地并行执行集**（claimedIds in-flight ∪
  本 tick 已选）而非仅单 tick——fire-and-forget 下跨 tick 并行否则分级失效；high 需
  空集且选中后 break 本批，medium 并行集内限 1，low 自由。
- 设计教训：把"批"理解成单 tick 会让 5 个 medium 在 5 个 tick 内全部并行——约束的
  语义单位是并行集，不是轮询周期。
- TS 基线 165→171（scheduler 分级 3 + claim-loop gating 3）。

## C6 — D7 发布说明（217c49f）

- docs/architecture/durable-runtime-upgrade-notes.md：D7 四要点 + C1 reaper 窗口
  合并记录。事实核对过 contract 握手（T1 #637）、stale_dispatch_dropped 链路
  （worker 心跳→gateway 聚合→dashboard_service.rs）、T2 导入路径（main.rs）。

## C7 — review 死代码处置（7d3b207）

- 删生产端（reviewSubtask 管线、parseReviewOutput、双死 prompt、subtask_reviewing
  事件面、Review rejected 短路、enableReview/reviewTimeoutMs 配置）；review 字段
  保留（类型内联）供 UI 渲染历史缓存与未来 Rust 重立。progress-widget wave tag 清除。
- **意外收获（真实 bug）**：claim-loop cancel 测试在 C7 微任务排布变化后暴露
  TaskStore.save 共享 `<id>.json.tmp` 的写-写竞争——cancelTask 的 persist 与
  in-flight runClaimed 的 outcome persist 并发，第一次 rename 消耗 tmp 后第二次
  ENOENT。修为 per-call 唯一 tmp 名（tmpSeq + 时间戳）。教训：共享 tmp 文件名的
  原子写在有并发写者时是错的；测试失败先找时序变化的根因，可能挖出潜伏 bug。

## 门禁终值与剩余

- TS：bun test 162 pass / 17 files；tsc 本包零错误（18 vendor 噪音预存）；selfcheck ALL PASS。
- pytest 全量后台跑（977+4 基线）；Docker/PG 宕机 → 菱形 PG 实跑与 PG 集成测试仍未执行
  （恢复跑法：手动/管理员启动 Docker Desktop 后 `cargo test -p uc-grpc --all-features
  --test pause_grace_diamond -- --ignored` 及 uc-engine graph_store_integration）。
- 剩余：pytest 结果确认 → 归档任务 → journal → 关闭 #642。
