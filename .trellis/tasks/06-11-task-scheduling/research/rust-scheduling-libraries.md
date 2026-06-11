# Research: Rust Scheduling Libraries

- **Query**: Compare tokio-cron-scheduler, apalis, fang, and custom tokio::time solutions for scheduling in UltimateCoders
- **Scope**: Mixed (internal codebase + external libraries)
- **Date**: 2026-06-11

## Findings

### Existing Codebase Context

| File Path | Description |
|---|---|
| `crates/uc-engine/src/scheduler.rs` | Placeholder scheduler module (delegates to `scheduler/orchestrator.rs`) |
| `crates/uc-engine/src/scheduler/orchestrator.rs` | Placeholder -- "Will be implemented in PR6" |
| `crates/uc-engine/src/local.rs` | LocalEngine with tokio async runtime, no scheduling yet |
| `crates/uc-engine/src/events.rs` | EventStore trait + NatsEventStore (JetStream-backed) |
| `crates/uc-engine/Cargo.toml` | Current deps: tokio, async-nats (feature-gated), chrono, sqlx (feature-gated) |
| `crates/uc-types/src/agent.rs` | Task/Subtask/AgentEvent types, TaskStatus enum (has Paused) |
| `Cargo.toml` (workspace) | tokio 1 (full), async-nats 0.38, sqlx 0.8 (postgres), chrono 0.4 |

Key observations from codebase:
- The Rust engine already runs on tokio with `features = ["full"]`
- `async-nats` 0.38 is in workspace deps but the **latest `async-nats` is 0.43** (needed for NATS 2.12+ schedule headers)
- `sqlx` with PostgreSQL is already a dependency -- useful for persistence
- The `scheduler/` module exists but is entirely placeholder
- `TaskStatus::Paused` already exists -- natural fit for deferred/scheduled tasks
- `chrono` with serde is already in use throughout the codebase

---

### Library Comparison

#### 1. tokio-cron-scheduler (v0.15.1)

- **Repo**: https://github.com/mvniekerk/tokio-cron-scheduler (719 stars, actively maintained)
- **Cron support**: Full cron expressions via `croner` crate. Also supports English-language scheduling ("every 4 seconds") with optional `english` feature.
- **Timezone**: Supports timezone-aware scheduling via `chrono-tz` (0.10). Jobs can be created with `_tz` variants.
- **Async**: Fully async-native. Built specifically for tokio. Jobs are `Job::new_async()` returning `Box::pin(async move { ... })`.
- **Persistence**: **Yes -- PostgreSQL and NATS JetStream storage backends** (feature-gated: `postgres_storage`, `nats_storage`). Uses `tokio-postgres` directly (not sqlx). Persistence uses protobuf (`prost`) for serialization.
- **Job types**: Cron jobs, one-shot (`Job::new_one_shot`), repeated at fixed interval (`Job::new_repeated`).
- **Notifications**: `on_start_notification_add`, `on_stop_notification_add`, `on_removed_notification_add` callbacks per job.
- **Shutdown**: Ctrl-C signal handling (`shutdown_on_ctrl_c`), custom shutdown handlers.
- **Integration**: Simple API -- `JobScheduler::new().await`, `sched.add(job).await`, `sched.start().await`.
- **Limitations**:
  - Uses `tokio-postgres` directly, not `sqlx` -- would need a separate connection pool
  - NATS storage feature uses `async-nats` 0.43 (newer than project's 0.38)
  - No built-in concept of "night window" or load-aware scheduling
  - No task prioritization
  - Protobuf serialization for persistence adds build dependency (`prost-build`)
- **Verdict**: Best fit for the project's cron + tokio requirements. The NATS storage option is directly relevant since the project already uses NATS JetStream.

#### 2. apalis (v1.0.0-rc.9)

- **Repo**: https://github.com/geofmureithi/apalis (1,241 stars, actively maintained)
- **Description**: "Simple, extensible, and high-performance background processing library for Rust"
- **Architecture**: Job queue system with tower-based middleware. More like Celery than a cron scheduler.
- **Cron support**: No built-in cron scheduling in the current version. The `apalis-cron` crate referenced in old README does not exist in the current repo structure. Scheduling would need to be built on top or triggered externally.
- **Async**: Runtime-agnostic (works with tokio, async-std). Uses `futures-timer` for sleep.
- **Persistence**: Built-in support for Redis, PostgreSQL, SQLite, MySQL via separate crates (`apalis-sql`). PostgreSQL persistence uses `sqlx` -- **compatible with the project's existing sqlx dependency**.
- **Features**: Task prioritization, retries, rate limiting, timeout, unique jobs, graceful shutdown, monitoring, web UI (`apalis-board`).
- **Middleware**: Full tower middleware ecosystem. Extensible with custom layers.
- **Limitations**:
  - **No cron scheduling** -- would need external trigger mechanism
  - v1.0.0-rc.9 -- still pre-release
  - More complex setup than tokio-cron-scheduler
  - Designed for job queue processing, not time-based scheduling
- **Verdict**: Better suited as a job queue / worker pool. Would need a separate scheduler (like tokio-cron-scheduler or NATS scheduling) to trigger cron jobs into the apalis queue. Could be complementary but is not a scheduling solution on its own.

#### 3. fang (v0.11.0-rc1)

- **Repo**: https://github.com/ayrat555/fang (716 stars)
- **Description**: "Background task processing library for Rust"
- **Cron support**: Yes -- periodic (CRON) tasks using cron expressions.
- **Async**: Both async and threaded workers. Async workers run as tokio tasks.
- **Persistence**: PostgreSQL, SQLite, MySQL backends. Uses `tokio-postgres` for async mode.
- **Scheduling**: Tasks can be scheduled at a specific time in the future. Unique tasks to prevent duplication.
- **Retries**: Custom backoff mode.
- **Limitations**:
  - Smaller community, less active maintenance
  - RC version status (0.11.0-rc1)
  - Less ergonomic API than alternatives
  - Does not support NATS as a backend
  - Cron expressions supported but not timezone-aware (runs in UTC only)
  - No built-in load-awareness or time-window support
- **Verdict**: Functional but less mature than alternatives. The PostgreSQL persistence is useful, but the lack of timezone support and NATS integration makes it less suitable.

#### 4. Custom tokio::time Solution

- **Approach**: Use `tokio::time::interval`, `tokio::time::sleep_until`, or `tokio::time::Instant` directly.
- **Cron support**: Would need to implement cron expression parsing from scratch or use `croner` crate separately.
- **Persistence**: Must be implemented manually -- store scheduled tasks in PostgreSQL, load on startup.
- **Async**: Native tokio, zero overhead.
- **Complexity**: Low for simple fixed-interval scheduling, high for cron + persistence + recovery.
- **Advantages**:
  - Zero additional dependencies
  - Full control over behavior
  - Can integrate directly with existing `sqlx` pool and `async-nats` client
  - Can implement project-specific features (night windows, load awareness, timezone) without library constraints
- **Disadvantages**:
  - Significant implementation effort for cron parsing, timezone handling, persistence, and recovery
  - Must handle edge cases (missed schedules on restart, overlapping executions, DST transitions)
  - No community battle-testing
- **Verdict**: Best for simple interval-based scheduling. For cron + persistence, the effort is substantial and risks bugs.

---

### Comparison Matrix

| Feature | tokio-cron-scheduler | apalis | fang | Custom tokio::time |
|---|---|---|---|---|
| Cron expressions | Yes (croner) | No (separate crate needed) | Yes | Must implement |
| Timezone support | Yes (chrono-tz) | N/A | No | Must implement |
| Async-native | Yes (tokio) | Yes (runtime-agnostic) | Yes (tokio) | Yes (tokio) |
| PostgreSQL persistence | Yes (tokio-postgres) | Yes (sqlx) | Yes (tokio-postgres) | Must implement |
| NATS persistence | Yes (async-nats) | No | No | Must implement |
| One-shot jobs | Yes | Yes | Yes | Yes |
| Interval jobs | Yes | No (queue-based) | No | Yes |
| Task prioritization | No | Yes | No | Can implement |
| Retry/backoff | No | Yes (tower) | Yes | Must implement |
| Production readiness | Good | Pre-release (RC) | RC version | Custom code |
| Integration effort | Low | Medium-High | Medium | High (for cron) |

### Integration Considerations for UltimateCoders

1. **tokio-cron-scheduler** aligns most closely with the project's needs because:
   - It supports both cron expressions and one-shot/interval scheduling
   - It has NATS JetStream persistence (the project already uses NATS)
   - It has timezone-aware scheduling via chrono-tz
   - It is async-native on tokio

2. **Version conflict**: The project uses `async-nats = "0.38"`, but tokio-cron-scheduler depends on `async-nats = "0.43"`. The workspace `async-nats` version should be upgraded to support the NATS 2.14 schedule headers (which are in 0.43+).

3. **PostgreSQL persistence**: tokio-cron-scheduler uses `tokio-postgres` directly rather than `sqlx`. The project already has `sqlx` for PostgreSQL. This means two PostgreSQL connection pools would coexist. This is acceptable but not ideal.

4. **Night window scheduling**: None of the libraries have built-in time-window support. This would need to be implemented as a custom layer:
   - Store night window configuration (e.g., `22:00-06:00 UTC`) in PostgreSQL
   - Before executing a scheduled job, check if current time falls within the window
   - If outside window, defer execution (reschedule or hold in queue)

5. **Combining approaches**: A practical architecture could be:
   - Use **tokio-cron-scheduler** for time-based triggers (cron + one-shot)
   - Use **PostgreSQL** for schedule configuration and execution history
   - Use **NATS JetStream** for triggering scheduled tasks across nodes
   - Implement night-window logic as a middleware/guard in the scheduler

### External References

- [tokio-cron-scheduler docs](https://docs.rs/tokio_cron_scheduler/) -- official documentation
- [tokio-cron-scheduler GitHub](https://github.com/mvniekerk/tokio-cron-scheduler) -- source and examples
- [croner crate](https://docs.rs/croner/) -- underlying cron expression parser
- [apalis GitHub](https://github.com/geofmureithi/apalis) -- background job processing
- [fang GitHub](https://github.com/ayrat555/fang) -- background task processing
- [tokio::time module](https://docs.rs/tokio/latest/tokio/time/index.html) -- native time primitives

## Caveats / Not Found

- tokio-cron-scheduler's NATS storage feature was not tested for compatibility with the project's existing NatsEventStore implementation
- apalis-cron appears in older documentation but does not exist in the current apalis repository structure; it may have been removed or is planned for a future release
- fang's timezone handling could not be fully verified from documentation alone
- Custom tokio::time implementation cost estimates are approximate
