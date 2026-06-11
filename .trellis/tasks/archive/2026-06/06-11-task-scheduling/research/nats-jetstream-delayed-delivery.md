# Research: NATS JetStream Delayed Delivery

- **Query**: Can NATS JetStream be used as a scheduling mechanism via delayed message delivery? What are the trade-offs vs dedicated scheduler?
- **Scope**: Mixed (internal codebase + external NATS documentation)
- **Date**: 2026-06-11

## Findings

### Existing NATS Infrastructure in UltimateCoders

| File Path | Description |
|---|---|
| `crates/uc-engine/src/events.rs` | `NatsEventStore` implementation using `async_nats::jetstream::Context` |
| `crates/uc-engine/Cargo.toml` | `async-nats = { workspace = true, optional = true }` with `messaging` feature |
| `Cargo.toml` (workspace) | `async-nats = "0.38"` |
| `crates/uc-engine/src/events.rs:204-234` | `NatsEventStore::new()` creates `AGENT_EVENTS` stream with `agent.events.>` subject |

Key observations:
- The project already has a `NatsEventStore` that publishes to JetStream
- The `AGENT_EVENTS` stream uses subject `agent.events.>`
- The `async-nats` version is 0.38, which predates NATS 2.12 schedule header support
- The `NatsEventStore` currently only uses basic publish/consume, not any scheduling features

---

### NATS 2.12 Delayed Message Scheduling

**Introduced in**: NATS Server v2.12 (released 2024)

**Feature**: Messages can be scheduled for future delivery by including a `Nats-Schedule` header. The server holds the message and delivers it to consumers only after the specified time.

**From the NATS 2.12 release blog**:
> "Another frequently requested feature is being able to schedule a message to be consumed later. You can now schedule a message to be automatically published after a delay. Enabling various patterns where a consumer should not immediately act on a message, for example in the form of job scheduling."

**Header fields** (from `async-nats` 0.43+ `header.rs`):

| Header | Constant | Description |
|---|---|---|
| `Nats-Schedule` | `NATS_SCHEDULE` | Schedule expression for a JetStream message scheduler entry (ADR-51) |
| `Nats-Schedule-Target` | `NATS_SCHEDULE_TARGET` | Target subject the schedule publishes to |
| `Nats-Schedule-Ttl` | `NATS_SCHEDULE_TTL` | TTL applied to messages produced by the schedule |
| `Nats-Schedule-Source` | `NATS_SCHEDULE_SOURCE` | Source subject sampled into the schedule output |
| `Nats-Schedule-Time-Zone` | `NATS_SCHEDULE_TIME_ZONE` | Time zone for cron schedules (IANA names like `America/New_York`) |
| `Nats-Schedule-Rollup` | `NATS_SCHEDULE_ROLLUP` | Auto-applies a rollup on the schedule target (`sub` only) |
| `Nats-Scheduler` | `NATS_SCHEDULER` | On schedule-produced messages: the subject of the originating schedule |
| `Nats-Schedule-Next` | `NATS_SCHEDULE_NEXT` | On schedule-produced messages: timestamp of next firing or `purge` for delayed schedules |

**Current capabilities**:
- **Single delayed message scheduling**: A message can be scheduled to be delivered after a specific delay
- **Cron-like scheduling**: The design document (ADR-51) includes future support for repeated message scheduling with cron-like expressions, but this is **not yet implemented** in the server
- **Timezone support**: `Nats-Schedule-Time-Zone` header accepts IANA timezone names

**Future capabilities** (from the release blog):
> "The server only supports single delayed message scheduling today, but the design already includes how this could potentially be extended in the future to perform repeated message scheduling, for example with Cron-like schedules."

### NATS 2.14 Enhancements

From the `async-nats` 0.43 commit history:
- Added `NATS_SCHEDULE`, `NATS_SCHEDULE_TARGET`, `NATS_SCHEDULE_TTL`, `NATS_SCHEDULE_TIME_ZONE`, `NATS_SCHEDULE_ROLLUP`, `NATS_SCHEDULER`, `NATS_SCHEDULE_NEXT` header constants
- Added `NATS_BATCH_ID`, `NATS_BATCH_SEQUENCE`, `NATS_BATCH_COMMIT` for atomic batch publishing
- Added `NATS_ROLLUP` for stream rollup
- Added `NATS_REQUIRED_API_LEVEL` for API version negotiation

These headers are **defined as constants** in `async-nats` 0.43+ but the actual scheduling **server-side logic** requires NATS Server 2.12+.

### Version Requirements

| Component | Required Version | Project Current Version | Action Needed |
|---|---|---|---|
| NATS Server | 2.12+ for delayed delivery, 2.14+ for cron schedules | Not specified in Docker Compose | Upgrade to 2.14+ |
| async-nats (Rust) | 0.43+ for schedule header constants | 0.38 | Upgrade to 0.43+ |
| nats.py (Python) | Needs 2.12+ server support | Not currently used | Would need to add |

---

### Using NATS JetStream as a Scheduling Mechanism

#### Approach A: Pure NATS Delayed Delivery

**How it works**:
1. To schedule a task for 2:00 AM, publish a message to a `scheduled.tasks` stream with the `Nats-Schedule` header set to the target delivery time
2. The NATS server holds the message until the scheduled time
3. At the scheduled time, the message becomes available to consumers
4. A consumer (the Orchestrator) picks up the message and submits the task

**Advantages**:
- No additional scheduler process needed
- Messages are durable (persisted in JetStream)
- Automatic failover if consumer disconnects (messages remain in stream)
- Leverages existing NATS infrastructure
- No separate persistence layer for schedule state (JetStream IS the persistence)

**Disadvantages**:
- **No cron support yet**: Only single delayed delivery. For recurring schedules, the consumer must re-publish the next scheduled message after handling the current one.
- **No time-window awareness**: The server delivers at the exact scheduled time, regardless of load or business rules. Time-window logic must be in the consumer.
- **No schedule management API**: Cannot list, modify, or cancel scheduled messages through the NATS API. Would need to manage this through a separate metadata store.
- **Version dependency**: Requires NATS Server 2.12+ and `async-nats` 0.43+.
- **Message ordering**: If a stream has both immediate and delayed messages, the delayed messages occupy sequence numbers but are not delivered until their scheduled time. This can affect consumer sequencing.

#### Approach B: NATS + PostgreSQL Hybrid

**How it works**:
1. Schedule definitions (cron expressions, night windows, task descriptions) are stored in PostgreSQL
2. The scheduler reads from PostgreSQL and publishes delayed messages to NATS at the appropriate time
3. NATS delivers the messages to consumers
4. Execution history is recorded in PostgreSQL

**Advantages**:
- Full cron support (computed by the scheduler, not NATS)
- Schedule management via PostgreSQL (list, modify, cancel)
- Night-window logic in the scheduler (before publishing to NATS)
- NATS handles the reliable delivery and consumer coordination
- PostgreSQL handles the configuration and history

**Disadvantages**:
- Two systems to manage (scheduler process + NATS)
- Scheduler is a single point of failure (unless made HA)
- More complex than pure NATS approach

#### Approach C: Dedicated Scheduler + NATS for Triggers

**How it works**:
1. Use `tokio-cron-scheduler` (or similar) as the scheduling engine
2. When a scheduled task fires, publish a message to NATS JetStream
3. Consumers (Orchestrator instances) pick up the message and execute the task
4. Schedule state is persisted by the scheduler (PostgreSQL or NATS storage)

**Advantages**:
- Full cron support with timezone awareness
- Built-in persistence and recovery (tokio-cron-scheduler supports both PostgreSQL and NATS storage)
- NATS handles the distributed delivery to multiple Orchestrator instances
- Clean separation of concerns: scheduler decides WHEN, NATS delivers, Orchestrator executes

**Disadvantages**:
- Additional dependency (tokio-cron-scheduler)
- Scheduler process must be running (not purely event-driven)
- Potential for duplicate triggers if both scheduler persistence and NATS delivery have at-least-once semantics

---

### Trade-off Summary: NATS Delayed Delivery vs Dedicated Scheduler

| Aspect | NATS Delayed Delivery | Dedicated Scheduler (tokio-cron-scheduler) |
|---|---|---|
| Cron expressions | Not yet (future: ADR-51) | Yes (via croner) |
| One-shot scheduling | Yes (Nats-Schedule header) | Yes |
| Timezone support | Yes (Nats-Schedule-Time-Zone) | Yes (chrono-tz) |
| Persistence | Built-in (JetStream) | PostgreSQL or NATS storage |
| Recovery on restart | Automatic (messages in stream) | Depends on storage backend |
| Schedule management | No API (must manage externally) | Via storage backend |
| Night-window logic | Must be in consumer | Must be in scheduler |
| Distributed coordination | Built-in (JetStream consumers) | Via NATS storage backend |
| Infrastructure | NATS only | NATS + scheduler process |
| Maturity | New (NATS 2.12, 2024) | Mature (719 stars, active) |
| Version requirement | NATS Server 2.12+, async-nats 0.43+ | tokio 1.x, async-nats 0.43+ (for NATS storage) |

### Recommendation for UltimateCoders

**Use Approach C (Dedicated Scheduler + NATS for Triggers)** as the primary mechanism, with NATS delayed delivery as a complementary feature:

1. **tokio-cron-scheduler** handles the scheduling logic (cron parsing, timezone, persistence)
2. When a scheduled task fires, it publishes a message to NATS JetStream (e.g., `scheduled.tasks.fire`)
3. The Orchestrator (Python) subscribes to this subject and executes the task
4. For one-shot deferred tasks, NATS delayed delivery (`Nats-Schedule` header) can be used directly as an optimization
5. Night-window logic is implemented as a guard in the scheduler (check before publishing to NATS)

**Required upgrades**:
- `async-nats`: 0.38 -> 0.43+ (for schedule header constants and NATS storage compatibility)
- NATS Server: ensure Docker Compose uses 2.14+ image
- Add `tokio-cron-scheduler` dependency to `uc-engine/Cargo.toml`

### External References

- [NATS 2.12 Release Blog](https://nats.io/blog/nats-server-2.12-release/) -- delayed message scheduling announcement
- [NATS ADR-51](https://github.com/nats-io/nats-architecture/blob/main/adr/ADR-51.md) -- design document for message scheduling (referenced in release blog)
- [async-nats header.rs](https://github.com/nats-io/nats.rs/blob/main/async-nats/src/header.rs) -- schedule header constants (0.43+)
- [NATS JetStream docs](https://docs.nats.io/using-nats/jetstream/) -- JetStream concepts
- [NATS deferred message issue #3403](https://github.com/nats-io/nats-server/issues/3403) -- original feature request and discussion
- [tokio-cron-scheduler NATS storage](https://github.com/mvniekerk/tokio-cron-scheduler) -- persistence via NATS JetStream

## Caveats / Not Found

- The ADR-51 design document for cron-like repeated scheduling in NATS was referenced but not directly accessible for full content review
- The exact API for publishing delayed messages via `async-nats` 0.43+ was not verified from source code (the header constants exist, but the publish API may require specific method calls)
- NATS 2.14's cron schedule feature (if implemented) was not confirmed from server release notes -- the `Nats-Schedule` header constants suggest it may be in progress
- The interaction between NATS delayed delivery and the existing `NatsEventStore` stream (`AGENT_EVENTS`) was not analyzed -- a separate stream (e.g., `SCHEDULED_TASKS`) would likely be needed
- Performance characteristics of NATS delayed delivery under high schedule volume were not researched
