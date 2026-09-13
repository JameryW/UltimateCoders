# Research: PG 存储/迁移/导入面现状（T2 前置勘察，2026-09-12）

## 1. 迁移模式（T2 必须 follow 的样板）
- 样板 = `scheduler/migration.rs`：`pub async fn run_migrations(pool)` → `hold_schema_migrations_lock(pool, "scheduler")`（#631 的 `SchemaMigrationGuard`，**自持专用 PgConnection**，key `0x5543_4d4c` 全应用单锁，30s 轮询）→ `CREATE TABLE IF NOT EXISTS` + `ALTER … ADD COLUMN IF NOT EXISTS` + 索引数组循环。非 storage feature 有同名 stub。
- **无共享迁移入口**：每个 store 在自己构造函数里 migrate（`PostgresTaskBackend::new` task_store.rs L151-159 —— 注意它**没用** advisory lock，是历史遗留裸跑；`PostgresScheduleStore::connect` scheduler/store.rs L173；`PostgresMetadataStore::new` metadata/postgres.rs L90）。
- 启动装配点：`uc-grpc-server/src/main.rs`（resolve env L143-168 → backend 构造 L176/L280 → engine → `load_tasks_from_backend` L672）。GraphStore 应在 main.rs 同位置构造。
- 全仓零 `.sql` 文件，DDL 全是嵌入 `sqlx::query(r#""#)` 字符串。

## 2. 连接与 env 的既成事实（坑）
- **两套 env**：metadata 用 `UC_PG_URL`（config.rs L99，默认 localhost）；task/schedule backend 用 `UC_DATABASE_URL`（main.rs L225/L317）。compose 里 `UC_TASK_BACKEND=postgres`+`UC_PG_URL`。T2 用 `UC_DATABASE_URL`（与 task backend 同源）并在 PRD 记录此分裂。
- 连接失败语义：**构造返回 Ok + 内存 fallback**（pool:None + warn）→ 任何真 PG 测试必须 assert `is_connected()`，否则假绿。
- `PostgresTaskBackend` pool 无 `acquire_timeout`（metadata 有 10s）；真 PG 测试连接串必须 `127.0.0.1`（localhost 走 IPv6 每连 stall ~10s）。

## 3. 旧数据形状（导入源）
- PG `tasks` 表：id/description/project_id/status TEXT/`subtasks JSONB`(Vec<Subtask>)/created_at/updated_at（task_store.rs L197-205）。status↔str 映射在 L243-290。`update_task` 是 upsert。
- 读权威 = gateway 内存 HashMap（`load_from_backend` L701：`list_tasks` ORDER BY created_at DESC 全量入 map；写是 fire-and-forget persist）。
- `.uc/tasks/*.json`（TS PersistedTask，task-store.ts L15-65）：camelCase；subtasks 含 `dependsOn/files/result/review/startedAt/completedAt/retryCount/dispatchMode/requiredCapabilities/steps`；顶层 `controlState(resumeFromWave/redecomposed)/savedAt`。checkpoint 文件在 `.uc/checkpoints/`，restore 取 savedAt 较新者 → **导入时同样取 task 文件 vs checkpoint 的较新者**。
- 类型：`TaskId(pub String)` newtype；**无 SubtaskId**——Subtask.id/parent_id/depends_on 全是 TaskId（agent.rs L78-85）。serde 即裸字符串，graph_id=task_id / node_id=subtask_id 恒等映射（T1 已定）在数据层无碰撞（TS 生成 id 形如 `uc-<n>-<base36>`、拆分后缀 `-fN/-pN`）。
- `agent_events` 表只有 DDL 无任何读写方（grep 证实）——execution_events 新表设计时不要继承它的悬空列。

## 4. 挂点
- EngineApi 任务 CRUD：submit/get/list/pause/resume（engine.rs L161-177），实现方 LocalEngine/GrpcEngineClient；网关 TaskService 走独立 gRPC TaskStore（server.rs L445），不经 EngineApi。
- 影子读应挂在 gRPC TaskStore 读路径（server.rs）+ 写侧在 `persist_task` 旁路写图表；T6 前 HashMap 仍是行为权威。
- uc-types 新 pub 类型必须显式加 lib.rs `pub use`（E0422 旧坑）。
