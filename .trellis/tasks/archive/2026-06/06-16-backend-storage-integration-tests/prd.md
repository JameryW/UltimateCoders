# Backend Storage Integration Tests

## Goal

为 TiKV/Qdrant/PostgreSQL 真实存储后端添加集成测试，验证 fallback 测试无法覆盖的路径（连接、序列化、schema migration、端到端 CRUD）。

## Decision: Docker Compose + #[ignore] 标记

- 写 `#[cfg(feature = "storage")]` + `#[ignore]` 的集成测试
- 本地手动跑：`docker compose up -d` → `cargo test --features storage -- --ignored`
- CI 新增 job：先 `docker compose up -d`，等 healthcheck，再跑 `--ignored`
- 复用已有 docker-compose.yml，零新依赖

## Requirements

### AC1: PostgreSQL 集成测试

- `PostgresMetadataStore::new(pg_url)` 连接成功
- `run_migrations()` 创建所有表（repos, index_state, symbols, references + 新增 count 列）
- repo CRUD：insert → get → update → list → delete
- symbol CRUD：insert → search by name/kind/file → delete
- index_state CRUD：insert → get → update (with counts) → get (verify counts)
- 测试环境变量：`UC_PG_URL`（默认 `postgresql://ultimate_coders:ultimate_coders@localhost:5432/ultimate_coders`）

### AC2: Qdrant 集成测试

- `LongTermMemory::new(qdrant_url)` 连接成功 + collection 自动创建
- write → search (with BLAKE3 embedding) → delete
- scope filtering：write project-scoped → search with project scope → verify results
- 测试环境变量：`UC_QDRANT_URL`（默认 `http://localhost:6334`）

### AC3: TiKV 集成测试

- `ShortTermMemory::new(pd_endpoints)` 连接成功
- write → read → delete → list_keys (prefix scan)
- TTL 过期验证（写入短 TTL 条目 → 等待 → 读不到）
- 测试环境变量：`UC_TIKV_PD_ENDPOINTS`（默认 `localhost:2379`）

### AC4: MemoryStore 端到端集成测试

- `MemoryStore` + `EmbeddingService` 完整链路：write(high importance) → search_memory → 返回结果
- `read()` with `include_semantic=true` → 命中 long-term
- short-term miss + long-term hit 的降级路径

### AC5: CI 集成测试 job

- 新增 GitHub Actions job：`storage-integration`
- 步骤：docker compose up → wait healthchecks → cargo test --features storage -- --ignored → docker compose down
- 仅在 `crates/**` 或 `docker-compose.yml` 变更时触发
- 失败时打印 docker compose logs

## Acceptance Criteria

- [ ] `cargo test --features storage -- --ignored` 在 Docker Compose 环境下全部通过
- [ ] PostgreSQL 集成测试覆盖 migration + CRUD + counts
- [ ] Qdrant 集成测试覆盖 write/search/delete + scope
- [ ] TiKV 集成测试覆盖 write/read/delete/list + TTL
- [ ] MemoryStore 端到端测试覆盖 search_memory + read semantic
- [ ] CI storage-integration job 可运行（手动触发）
- [ ] 所有测试标记 `#[ignore]`，不影响默认 `cargo test`

## Definition of Done

- Tests added (integration tests in `tests/` directory)
- CI job added in `.github/workflows/ci-rust.yml`
- `cargo test -p uc-engine`（不含 --ignored）仍全部通过
- `cargo clippy` + `cargo fmt --check` clean

## Out of Scope

- NATS 集成测试（留到消息分发修复时再做）
- 性能/压力测试
- 并发安全测试
- Docker-in-Docker（用原生 Docker Compose）
- testcontainers-rs 引入

## Technical Notes

- 测试文件：`crates/uc-engine/tests/storage_integration.rs`（新文件）
- Docker Compose 已有 healthcheck，可用 `docker compose up -d --wait` 等待
- TiKV 需要 PD + KV 两个容器，PD healthcheck 先通过
- 环境变量统一前缀 `UC_`，与 docker-compose.yml 中 orchestrator 的 env 对齐
- 所有测试使用唯一 key/prefix 避免并行冲突（`format!("test_{}_{}", module_path!(), uuid)`)
