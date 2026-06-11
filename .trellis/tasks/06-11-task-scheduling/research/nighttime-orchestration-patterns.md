# Research: Night-time Orchestration Patterns

- **Query**: How do CI/CD runners, batch processing systems, and similar distributed systems handle off-peak scheduling? What patterns exist for time-window-based execution with load awareness?
- **Scope**: External (industry patterns and system designs)
- **Date**: 2026-06-11

## Findings

### Pattern 1: Time-Window Guard (Conditional Execution)

The most common pattern is a **time-window guard** that wraps task execution. The scheduler triggers at the scheduled time, but before executing, it checks whether the current time falls within the allowed execution window.

**Implementation pattern**:
```
Scheduled Trigger -> Time Window Check -> Execute or Defer
```

This is used by:
- **GitLab Runner**: Scheduled pipelines can be configured with `only:`/`except:` rules that include time-based conditions. CI/CD pipelines are often scheduled for off-peak but the runner checks resource availability before starting.
- **Kubernetes CronJobs**: Support `startingDeadlineSeconds` and `concurrencyPolicy`. While not time-window-aware natively, operators commonly add admission webhooks that reject job creation outside configured windows.
- **Apache Airflow**: Uses `TimeDeltaSensor` or `DateTimeSensor` to wait until a specific time before downstream tasks execute. The `ShortCircuitOperator` can be used as a time-window guard.

**Key design considerations**:
- What happens when the window closes while a task is running? (Allow completion vs. pause/cancel)
- What happens when a scheduled trigger fires outside the window? (Defer to next window vs. skip)
- How to handle windows that cross midnight (e.g., 22:00-06:00)
- Timezone handling: all times should be stored in UTC, converted to local time for display

### Pattern 2: Load-Aware Queue with Priority

Rather than strict time windows, some systems use a **priority queue with load awareness**. Night-time tasks get elevated priority during off-peak hours but can run at any time if resources are available.

**Implementation pattern**:
```
Task Queue -> Priority Calculator (load + time) -> Worker Pool
```

This is used by:
- **Buildkite**: Agents have priority and concurrency limits. Scheduled builds get queued with specific priority levels. The scheduler does not enforce time windows but allows priority-based scheduling.
- **GitHub Actions**: `workflow_dispatch` with `concurrency` groups. Scheduled workflows (`schedule` trigger with cron) run on a best-effort basis. No explicit time-window enforcement, but the concurrency system prevents resource conflicts.
- **AWS Batch**: Job queues with priority levels. Compute environments can scale to zero during peak hours and scale up during off-peak. The scaling policy acts as an implicit time window.

**Key design considerations**:
- How to measure current load? (active workers, queue depth, resource utilization)
- How to determine task priority dynamically? (static priority + time bonus)
- How to prevent starvation of low-priority tasks?
- How to handle burst scheduling (many tasks become eligible at window start)?

### Pattern 3: Deferred Queue / Delayed Execution

Tasks are submitted with a "not before" timestamp and held in a deferred queue until that time arrives. This is the simplest pattern for one-shot scheduled tasks.

**Implementation pattern**:
```
Submit Task (with execute_after timestamp) -> Deferred Queue -> Timer Check -> Move to Active Queue -> Execute
```

This is used by:
- **Celery**: `eta` and `countdown` parameters on `apply_async()`. Tasks sit in the broker until the ETA arrives.
- **NATS JetStream** (2.12+): Delayed message scheduling. Messages can be published with a `Nats-Schedule` header specifying when they should become deliverable.
- **RabbitMQ**: Delayed message exchange plugin. Messages are routed through a delayed exchange and become available after the specified delay.
- **PostgreSQL**: `SELECT ... WHERE execute_after <= NOW()` polling pattern. Common in job queue implementations (including apalis, fang, and custom solutions).

**Key design considerations**:
- Polling frequency vs. event-driven notification
- Recovery after restart (must re-scan deferred queue)
- Clock drift across distributed nodes
- Index efficiency on `execute_after` column

### Pattern 4: Maintenance Window Scheduler

Dedicated maintenance window schedulers are common in systems that need to run expensive background operations (index rebuilding, data compaction, security scans) during low-traffic periods.

**Implementation pattern**:
```
Maintenance Window Config -> Schedule Generator -> Queue -> Workers -> Completion Tracking
```

This is used by:
- **Elasticsearch**: ILM (Index Lifecycle Management) policies with `min_age` and scheduled rollups. Force merges and snapshots are scheduled during off-peak.
- **PostgreSQL**: `pg_cron` extension for scheduling maintenance operations (VACUUM, ANALYZE, reindex) during specific time windows.
- **Redis**: `FAILOVER` commands can be scheduled for maintenance windows. Background save operations use `save` scheduling.
- **Datadog/Azure Monitor**: Scheduled maintenance windows for synthetic tests and data rollups.

**Key design considerations**:
- Idempotency: maintenance tasks must be safe to re-run
- Progress tracking: long-running maintenance tasks need checkpoint/resume
- Resource limits: maintenance tasks should not consume all available resources
- Graceful degradation: if the window is too short, partial progress must be preserved

### Pattern 5: Event-Driven with Backpressure

Rather than explicit time windows, some systems use event-driven architecture with backpressure. When the system detects low load (via metrics), it triggers maintenance tasks. When load increases, it throttles or pauses them.

**Implementation pattern**:
```
Metrics Collector -> Load Analyzer -> Task Dispatcher -> Workers (with backpressure)
```

This is used by:
- **Kubernetes HPA + Descheduler**: The descheduler evicts pods based on node utilization, running more aggressively during low-utilization periods.
- **Auto-scaling batch systems**: AWS Batch, GCP Cloud Run Jobs scale compute environments based on queue depth and cost constraints (spot instance availability, time-of-day pricing).
- **ChromaDB/Qdrant**: Background index optimization triggered by segment count thresholds, not time-based schedules.

**Key design considerations**:
- Metric lag: load metrics may be delayed, leading to incorrect decisions
- Hysteresis: need cooldown periods to prevent oscillation
- Minimum window: even with load-based triggering, a minimum time window prevents premature execution

---

### Applicable Patterns for UltimateCoders

Based on the project's existing architecture and requirements:

**Best fit: Pattern 1 (Time-Window Guard) + Pattern 3 (Deferred Queue)**

1. **Time-Window Guard** directly maps to the PRD requirement for "night-time orchestration." The system defines a configurable time window (e.g., 22:00-06:00 UTC), and scheduled tasks only execute within this window.

2. **Deferred Queue** leverages the existing NATS JetStream infrastructure. Tasks submitted with `execute_after` timestamps can be held in a NATS stream with delayed delivery (NATS 2.12+ feature). When the delivery time arrives and falls within the night window, the task is dispatched to the Orchestrator.

3. **Pattern 2 (Load-Aware Priority)** could be added as an enhancement. The existing `WorkerInfo.current_load` and `WorkerInfo.max_capacity` fields provide the load signals. A priority calculator could boost night-task priority during off-peak hours while allowing real-time tasks to take precedence during peak hours.

**Proposed architecture**:

```
[Scheduled Task Submission]
        |
        v
[Schedule Store (PostgreSQL)]  <-- cron config, execute_after, night_window
        |
        v
[Scheduler (Rust, tokio-cron-scheduler)]  <-- evaluates cron, checks time window
        |
        v
[NATS JetStream (delayed message)]  <-- deferred delivery to night window
        |
        v
[Orchestrator (Python)]  <-- submit_task(), decompose, assign to Workers
        |
        v
[Workers]  <-- execute subtasks
        |
        v
[Execution History (PostgreSQL)]  <-- record results
```

**Night window configuration schema**:
```sql
CREATE TABLE scheduled_tasks (
    id UUID PRIMARY KEY,
    description TEXT NOT NULL,
    project_id TEXT,
    cron_expression TEXT,           -- NULL for one-shot tasks
    execute_after TIMESTAMPTZ,      -- for one-shot deferred tasks
    night_window_start TIME,        -- e.g., 22:00 UTC
    night_window_end TIME,          -- e.g., 06:00 UTC
    timezone TEXT DEFAULT 'UTC',    -- IANA timezone name
    enabled BOOLEAN DEFAULT TRUE,
    last_execution TIMESTAMPTZ,
    next_execution TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE execution_history (
    id UUID PRIMARY KEY,
    scheduled_task_id UUID REFERENCES scheduled_tasks(id),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL,           -- 'completed', 'failed', 'skipped', 'deferred'
    result_summary TEXT,
    deferred_reason TEXT            -- e.g., 'outside_night_window', 'load_too_high'
);
```

### External References

- [GitLab CI/CD schedules](https://docs.gitlab.com/ee/ci/pipelines/schedules.html) -- cron-based pipeline scheduling
- [Kubernetes CronJobs](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/) -- time-based job execution
- [Apache Airflow scheduling](https://airflow.apache.org/docs/apache-airflow/stable/concepts/scheduler.html) -- DAG-based workflow scheduling
- [NATS 2.12 delayed message scheduling](https://nats.io/blog/nats-server-2.12-release/) -- native delayed delivery in JetStream
- [pg_cron](https://github.com/citusdata/pg_cron) -- PostgreSQL cron scheduler extension
- [Celery scheduling](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html) -- periodic task scheduling

## Caveats / Not Found

- Specific implementation details of GitHub Actions' internal scheduling system are not publicly documented
- Load-aware scheduling patterns in AI/ML batch processing systems were not specifically researched (focused on CI/CD and database maintenance)
- The interaction between night-window scheduling and the project's existing Circuit Breaker pattern was not analyzed in depth
- Cost optimization patterns (e.g., spot instance scheduling) were considered out of scope
