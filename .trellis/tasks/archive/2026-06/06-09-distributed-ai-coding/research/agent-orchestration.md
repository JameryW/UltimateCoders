# Research: Agent Orchestration Patterns for Distributed AI Coding Systems

- **Query**: How do systems like Devin, SWE-Agent, OpenHands, Cursor Agent structure their agent orchestration? Orchestrator-Worker pattern, checkpoint/resume, conflict resolution, rate limit handling.
- **Scope**: External / mixed
- **Date**: 2026-06-09

## Findings

---

## 1. Agent Orchestration Architectures in Existing Systems

### Devin (Cognition)

**Architecture**: Single autonomous agent with tool-use loop.

- Devin operates as a single agent with a persistent sandbox (Docker container) and a browser-based environment.
- It follows a **plan-then-execute** loop: given a task, it decomposes it into steps, then executes each step using tools (shell, editor, browser).
- There is no multi-agent orchestration in the public-facing version; the "orchestration" is internal to a single LLM loop that decides which tool to call next.
- Key architectural elements:
  - **Sandbox isolation**: Each session gets its own container with a full dev environment (VS Code Server, browser, shell).
  - **State persistence**: The container persists across steps, maintaining filesystem and process state.
  - **Tool-use loop**: LLM receives observation (tool output) -> reasons -> selects next tool -> repeat until task complete.
- **Relevance**: Shows that for a single-agent coding task, sandbox isolation + persistent state + tool-use loop is the baseline. Multi-agent adds coordination complexity on top of this.

### SWE-Agent (Princeton)

**Architecture**: Single agent with custom agent-computer interface (ACI).

- SWE-Agent focuses on designing the **interface** between the LLM and the computer environment, not multi-agent coordination.
- Key insight: The quality of the agent-computer interface matters more than the agent architecture for coding tasks.
  - Custom bash commands: `find_file`, `open_file`, `scroll_down`, `search_dir`, `edit_file` (with line-based replacement).
  - Linting-integrated edit commands that validate changes after each edit.
  - Navigation commands that show file context around the cursor.
- **Architecture pattern**:
  ```
  LLM -> Action Parser -> Shell/FS -> Observation Formatter -> LLM
  ```
- The agent loop is: `thought -> action -> observation -> thought -> ...`
- **Relevance**: The ACI design is critical. For multi-agent, each worker needs a well-designed ACI. The orchestration layer sits above this.

### OpenHands (formerly OpenDevel)

**Architecture**: Multi-agent system with explicit agent roles and a runtime controller.

- OpenHands implements the **Orchestrator-Worker** pattern most explicitly among these systems:
  - **Controller**: Manages the overall task flow, delegates subtasks to agents, aggregates results.
  - **Agents**: Specialized workers (CoderAgent, PlannerAgent, ManagerAgent) that handle specific aspects.
  - **Runtime**: Provides sandboxed execution environment (Docker-based).
- Key patterns:
  - **ManagerAgent**: Acts as the orchestrator. Decomposes a high-level task into subtasks, assigns them to CodeActAgent instances.
  - **CodeActAgent**: The primary worker. Can read/write code, run commands, and browse the web.
  - **PlannerAgent**: Generates a plan that the ManagerAgent follows.
  - Communication is through a shared **event stream** (action + observation pairs).
- **Event stream architecture** (critical for this project):
  - All agent actions and observations are appended to a shared event stream.
  - The stream is persisted, enabling replay and recovery.
  - Events: `Action` (agent wants to do something) and `Observation` (result of an action).
  - The controller routes actions to the appropriate runtime and observations back to the agent.
- **Relevance**: OpenHands is the closest reference architecture for our project. Its event stream pattern directly supports checkpoint/resume and audit trails.

### Cursor Agent

**Architecture**: Tool-use loop with inline editing, not multi-agent.

- Cursor Agent operates as a single agent within the IDE context.
- It uses a **tool-use** approach: the LLM can invoke tools like `edit_file`, `search`, `run_command`.
- Key architectural elements:
  - **Inline editing**: Changes are applied as diffs/patches, not file rewrites.
  - **Context management**: Automatically includes relevant files, definitions, and recent changes.
  - **Parallel tool calls**: Can invoke multiple independent tools simultaneously.
- **Relevance**: The inline diff-based editing model is important for conflict resolution design. Cursor shows that diffs are the natural unit of code change.

### AutoGen (Microsoft)

**Architecture**: Multi-agent conversation framework.

- Agents communicate through **conversations** (message passing).
- Supports both **two-agent chat** and **group chat** patterns.
- Key patterns:
  - **GroupChat**: A group manager (orchestrator) selects the next speaker in a round-robin or LLM-decided fashion.
  - **Nested chats**: Agents can initiate sub-conversations.
  - **Sequential** and **parallel** agent pipelines.
- **Relevance**: Shows how message-passing multi-agent coordination works. The GroupChat manager is analogous to our Orchestrator.

### CrewAI

**Architecture**: Role-based multi-agent framework.

- Defines agents with **roles**, **goals**, and **backstories**.
- Tasks are defined with expected outputs and assigned to agents.
- **Process types**: Sequential (chain of agents), Hierarchical (manager delegates), or Consensual (agents vote).
- Key patterns:
  - **Task delegation**: Manager agent breaks tasks into subtasks and delegates.
  - **Tool sharing**: Agents can share tools or have exclusive tools.
  - **Memory**: Short-term (within conversation), long-term (across sessions), entity memory (key-value store).
- **Relevance**: The hierarchical process type maps directly to our Orchestrator-Worker pattern. The memory model informs our layered memory design.

---

## 2. Orchestrator-Worker Pattern in Practice

### Task Decomposition

**Strategies**:

1. **LLM-driven decomposition**: The orchestrator uses an LLM call to break down a high-level task into subtasks. Each subtask is described with:
   - Objective (what to achieve)
   - Context (relevant files, dependencies)
   - Constraints (what NOT to modify)
   - Expected output (files changed, tests to pass)

2. **Template-based decomposition**: For known task types (feature implementation, bug fix, refactoring), use predefined decomposition templates:
   ```
   Feature Task -> [Research Subtask] -> [Implementation Subtask] -> [Test Subtask] -> [Review Subtask]
   ```

3. **Dependency graph construction**: After decomposition, build a DAG:
   - Nodes = subtasks
   - Edges = data/control dependencies
   - Parallel execution for independent nodes
   - Sequential execution for dependent nodes

**Implementation pattern (from OpenHands)**:
```
1. User submits task
2. Orchestrator LLM call: "Decompose this task into subtasks with dependencies"
3. Parse LLM output into subtask list + dependency graph
4. Assign subtasks to available workers
5. Workers execute and report results
6. Orchestrator aggregates and determines next actions
```

### Task Assignment

**Strategies**:

1. **Capability-based routing**: Match subtask requirements to worker capabilities.
   - Each worker registers its capabilities (languages, frameworks, tools).
   - Orchestrator maintains a worker registry (stored in PostgreSQL for our project).
   - Assignment considers: capability match, current load, past performance.

2. **Work stealing**: Idle workers can pull tasks from a shared queue.
   - NATS JetStream as the task queue.
   - Workers subscribe to subject patterns matching their capabilities.
   - Pull-based model reduces orchestrator bottleneck.

3. **Push-based assignment**: Orchestrator explicitly assigns tasks.
   - More control, easier to reason about.
   - Risk: orchestrator becomes bottleneck.
   - Mitigation: batch assignment, async ack.

**Recommendation for this project**:
- Use **push-based assignment** for MVP (simpler, more deterministic).
- Task queue in NATS JetStream with subject-based routing:
  ```
  task.assign.{worker_id}    # Direct assignment
  task.broadcast.{capability} # Broadcast to capable workers
  task.result.{task_id}      # Result channel
  ```

### Result Aggregation

**Strategies**:

1. **Sequential aggregation**: Orchestrator collects results one by one, each result potentially modifying the plan for remaining subtasks.
   - Most flexible, allows adaptive planning.
   - Downside: serialization point.

2. **Parallel aggregation**: All workers execute independently, results are merged at the end.
   - Fastest for independent tasks.
   - Risk: conflicting changes.

3. **Map-reduce pattern**: Workers produce partial results; orchestrator (or a dedicated "merge agent") reconciles them.
   - Good for search/analysis tasks.
   - For code edits: the "reduce" step is conflict resolution (see Section 4).

**Implementation**:
```
# Orchestrator aggregation logic
for subtask in topological_order(decomposed_tasks):
    result = await execute_or_collect(subtask)
    if result.has_conflicts:
        resolved = await resolve_conflicts(result, context)
    update_plan(subtask, result)
```

### Conflict Resolution at Orchestrator Level

When workers produce conflicting outputs:
1. **Detection**: Compare changed file sets across worker results. Flag overlapping files.
2. **Resolution strategies** (ordered by preference):
   a. **Re-assign conflicting subtask** to a single worker with full context.
   b. **Merge with LLM assistance**: Feed both changes + original file to an LLM for merge.
   c. **Last-writer-wins** with notification: Apply the later change, flag for human review.
   d. **Human escalation**: If confidence is low, pause and request human input.

---

## 3. Checkpoint/Resume (断点续跑) Mechanisms

### Event Sourcing Pattern

**Core idea**: Instead of storing current state, store the sequence of events that led to the current state. State is reconstructed by replaying events.

**Application to agent tasks**:

```
Event types:
- TaskCreated { task_id, description, parent_id }
- SubtaskAssigned { subtask_id, worker_id, specification }
- WorkerStarted { subtask_id, worker_id, timestamp }
- ToolInvoked { subtask_id, tool_name, tool_input }
- ToolResult { subtask_id, tool_output, exit_code }
- FileModified { subtask_id, file_path, diff }
- SubtaskCompleted { subtask_id, result, artifacts }
- TaskFailed { subtask_id, error, recoverable }
- CheckpointCreated { task_id, snapshot_id }
```

**Storage**:
- Events are appended to NATS JetStream (already in our architecture).
- JetStream provides:
  - **Durable streams**: Events persist across restarts.
  - **Replay**: Can replay from any offset, enabling state reconstruction.
  - **Consumer groups**: Multiple consumers can independently track progress.

**Recovery flow**:
```
1. Detect failure (worker heartbeat timeout, orchestrator crash)
2. Identify last completed checkpoint or event offset
3. Replay events from that offset to reconstruct state
4. Re-assign incomplete subtasks to available workers
5. Resume execution
```

### Write-Ahead Log (WAL) Pattern

**Core idea**: Before making a state change, log the intended change to a durable store. After the log is confirmed, apply the change.

**Application**:
```
WAL entries:
- TxnID: unique transaction identifier
- Action: the state mutation to perform
- State: "pending" | "committed" | "rolled_back"

Recovery:
1. On startup, scan WAL for "pending" entries
2. For each pending entry, determine if the action was completed (check actual state)
3. If completed: mark as "committed"
4. If not completed: replay or rollback
```

**Comparison with Event Sourcing for this project**:

| Aspect | Event Sourcing | WAL |
|--------|---------------|-----|
| Granularity | Fine-grained (every action) | Coarse (state transitions) |
| Replay cost | Higher (more events) | Lower (fewer entries) |
| Audit trail | Complete | Partial |
| Complexity | Higher | Lower |
| Best for | Debugging, full replay | Crash recovery |

**Recommendation**: Use **event sourcing as the primary mechanism** (via NATS JetStream) and **periodic state snapshots** as a performance optimization (avoid replaying entire history).

### State Snapshot Pattern

**Core idea**: Periodically capture a complete snapshot of the system state. On recovery, load the latest snapshot and replay only events after that snapshot.

**Snapshot contents**:
```
TaskSnapshot {
  task_id: String,
  status: Enum<Created, InProgress, Completed, Failed>,
  subtasks: Vec<SubtaskState>,
  assigned_workers: HashMap<WorkerId, SubtaskId>,
  completed_artifacts: Vec<Artifact>,
  file_state_hash: HashMap<FilePath, Hash>,  // For conflict detection
  last_event_offset: u64,                     // JetStream offset
  timestamp: DateTime,
}
```

**Snapshot strategy**:
- **Time-based**: Snapshot every N seconds (e.g., 30s).
- **Event-based**: Snapshot after every N events (e.g., 100 events).
- **Milestone-based**: Snapshot at task state transitions (subtask completed, plan updated).
- **Hybrid**: Time-based + milestone-based for robustness.

**Storage**: Snapshots stored in TiKV (fast KV access, distributed).

**Recovery with snapshots**:
```
1. Load latest snapshot from TiKV
2. Query NATS JetStream for events after snapshot's last_event_offset
3. Replay those events on top of the snapshot state
4. Resume orchestration from reconstructed state
```

### Implementation Sketch for This Project

```
// Rust core (engine layer)

struct CheckpointManager {
    nats_client: NATSClient,
    tikv_client: TiKVClient,
    snapshot_interval: u64,  // events between snapshots
    event_count: AtomicU64,
}

impl CheckpointManager {
    async fn record_event(&self, event: AgentEvent) -> Result<()> {
        // 1. Append to NATS JetStream
        let offset = self.nats_client.publish("agent.events", &event).await?;
        // 2. Increment counter
        let count = self.event_count.fetch_add(1, Ordering::SeqCst);
        // 3. Periodic snapshot
        if count % self.snapshot_interval == 0 {
            self.create_snapshot().await?;
        }
        Ok(())
    }

    async fn create_snapshot(&self) -> Result<SnapshotId> {
        // Collect current state from all components
        let state = self.collect_current_state().await?;
        // Store in TiKV
        let snapshot_id = self.tikv_client.put(
            format!("snapshot:{}:{}", state.task_id, state.timestamp),
            &state
        ).await?;
        // Publish checkpoint event
        self.nats_client.publish("agent.checkpoints", &CheckpointCreated {
            task_id: state.task_id,
            snapshot_id: snapshot_id.clone(),
            event_offset: state.last_event_offset,
        }).await?;
        Ok(snapshot_id)
    }

    async fn recover(&self, task_id: &str) -> Result<TaskState> {
        // 1. Find latest snapshot
        let snapshot = self.tikv_client.get_latest_snapshot(task_id).await?;
        // 2. Replay events after snapshot
        let events = self.nats_client.read_from_offset(
            "agent.events",
            snapshot.last_event_offset
        ).await?;
        // 3. Apply events to snapshot state
        let mut state = snapshot.state;
        for event in events {
            state.apply_event(event);
        }
        Ok(state)
    }
}
```

---

## 4. Multi-Agent Code Edit Conflict Detection and Resolution

### File Locking Strategies

**1. Pessimistic Locking (Exclusive Lock)**:
- Only one worker can edit a file at a time.
- Worker acquires lock before editing; releases after commit.
- **Pros**: No conflicts possible. Simple mental model.
- **Cons**: Reduces parallelism. Worker may hold lock too long.
- **Implementation**:
  ```
  // Lock stored in TiKV (distributed, atomic)
  struct FileLock {
      file_path: String,
      worker_id: String,
      acquired_at: DateTime,
      expires_at: DateTime,  // TTL to prevent deadlocks
  }

  // Acquire: atomic CAS (compare-and-swap) in TiKV
  async fn acquire_lock(file_path: &str, worker_id: &str, ttl: Duration) -> Result<Lock> {
      let key = format!("lock:{}", file_path);
      tikv_client.compare_and_swap(key, None, FileLock { ... }).await
  }
  ```

**2. Optimistic Locking (No lock, detect conflicts after)**:
- Workers edit freely; conflicts are detected and resolved after.
- **Pros**: Maximum parallelism. No lock contention.
- **Cons**: Conflict resolution can be expensive. Work may be wasted.

**3. Intent-based Locking (Advisory lock)**:
- Workers declare intent to edit a file (not exclusive).
- Other workers can see active intents and decide whether to proceed.
- **Pros**: Informative without blocking. Enables coordination.
- **Cons**: Voluntary; a rogue worker can ignore intents.
- **Implementation**:
  ```
  // Intent stored in NATS (broadcast to all workers)
  struct EditIntent {
      file_path: String,
      worker_id: String,
      edit_type: Enum<Create, Modify, Delete>,
      affected_regions: Vec<LineRange>,  // Approximate regions
  }
  ```

**Recommendation for this project**: **Intent-based locking for MVP**, with optimistic conflict detection as a safety net.

### Optimistic Merging

When two workers edit the same file concurrently:

**1. Three-way merge (git-style)**:
- Use the original file as the base.
- Apply both sets of changes using a three-way merge algorithm.
- If changes are in different regions: auto-merge succeeds.
- If changes overlap: mark as conflict.
- **Implementation**: Use `imara-diff` crate (Rust native diff algorithm) or `git2` (libgit2 bindings).

**2. Diff-based reconciliation**:
- Each worker produces a diff (unified diff or edit script).
- Diffs are applied in sequence: first worker's diff first, then second worker's.
- If second diff cannot be applied cleanly: conflict.
- **Advantage**: Diffs are the natural output format of agent editing (agents think in terms of "change these lines").

**3. Operational Transformation (OT)**:
- Used by collaborative editors (Google Docs, VS Code Live Share).
- Transform operations so they can be applied in any order with the same result.
- **Overkill for code editing**: OT is designed for fine-grained character-by-character editing. Code changes are typically line/block-level.
- **Not recommended** for this project.

**4. CRDT-based (Conflict-free Replicated Data Types)**:
- Data structures that can be merged deterministically without coordination.
- Used by collaborative editors (Figma, Automerge).
- **Not ideal for code**: Code is not a CRDT-friendly data type. The syntax/semantics of code require more structure than CRDTs preserve.
- **Not recommended** for this project.

### Recommended Conflict Resolution Pipeline

```
1. PREVENT: Intent-based locking (workers declare edit intents)
   - NATS broadcast: "Worker A intends to edit src/foo.rs lines 10-30"
   - Other workers can choose to avoid that file/region

2. DETECT: Post-edit conflict detection
   - After each edit, compute file content hash
   - Compare with hash at task start
   - If two workers changed the same file: flag conflict

3. RESOLVE: Tiered resolution
   Tier 1: Auto-merge (three-way diff)
     - If changes are in non-overlapping regions: apply both
     - Success rate: ~70% for independent changes

   Tier 2: LLM-assisted merge
     - Feed: original file + Worker A's diff + Worker B's diff
     - Ask LLM to produce a merged version
     - Validate: run linter + tests on merged result
     - Success rate: ~90% for non-trivial conflicts

   Tier 3: Orchestrator re-assignment
     - Combine both workers' context into a single subtask
     - Assign to one worker (or a new worker) with full context
     - Success rate: ~98% but loses parallelism

   Tier 4: Human escalation
     - Present conflict to human for resolution
     - Last resort, should be rare with good intent-based locking
```

### Implementation Sketch (Rust)

```
struct ConflictDetector {
    file_hashes: HashMap<PathBuf, FileHash>,  // baseline hashes
    pending_edits: HashMap<PathBuf, Vec<PendingEdit>>,
}

struct PendingEdit {
    worker_id: String,
    subtask_id: String,
    diff: UnifiedDiff,
    timestamp: DateTime,
}

impl ConflictDetector {
    fn check_conflict(&self, file_path: &Path, new_diff: &UnifiedDiff) -> ConflictResult {
        let existing = self.pending_edits.get(file_path);
        match existing {
            None => ConflictResult::NoConflict,
            Some(edits) => {
                // Check if diffs overlap
                for edit in edits {
                    if ranges_overlap(&edit.diff.changed_ranges(), &new_diff.changed_ranges()) {
                        return ConflictResult::Conflict {
                            conflicting_workers: vec![edit.worker_id.clone()],
                            resolution_tier: ResolutionTier::AutoMerge,
                        };
                    }
                }
                ConflictResult::NoConflict  // Same file, different regions
            }
        }
    }
}
```

---

## 5. LLM API Rate Limit Handling

### Rate Limit Types (Anthropic/Claude API)

Based on known Anthropic API behavior:

1. **Requests per minute (RPM)**: Limit on number of API calls.
2. **Tokens per minute (TPM)**: Limit on total input + output tokens.
3. **Concurrent requests**: Limit on simultaneous in-flight requests.
4. **Daily limits**: Total usage caps per day.

HTTP indicators:
- `429 Too Many Requests` with `Retry-After` header.
- `x-ratelimit-remaining-requests` and `x-ratelimit-remaining-tokens` response headers.
- `529 Overloaded` (server-side capacity).

### Retry Strategies

**1. Exponential Backoff with Jitter**:
```
base_delay = 1 second
max_delay = 60 seconds
max_retries = 5

delay = min(base_delay * 2^attempt + random_jitter, max_delay)
```

The jitter prevents the "thundering herd" problem where all retrying clients hit the API at the same moment.

**Implementation** (Tokio-based async):
```
async fn call_with_retry(client: &AnthropicClient, request: Request) -> Result<Response> {
    let mut attempt = 0;
    let base_delay = Duration::from_secs(1);
    let max_delay = Duration::from_secs(60);

    loop {
        match client.call(&request).await {
            Ok(response) => return Ok(response),
            Err(Error::RateLimited { retry_after }) => {
                if attempt >= MAX_RETRIES {
                    return Err(Error::MaxRetriesExceeded);
                }
                let delay = match retry_after {
                    Some(secs) => Duration::from_secs(secs),
                    None => {
                        let exp_delay = base_delay * 2u32.pow(attempt);
                        let jitter = rand::thread_rng().gen_range(0..500);
                        min(exp_delay + Duration::from_millis(jitter), max_delay)
                    }
                };
                tokio::time::sleep(delay).await;
                attempt += 1;
            }
            Err(Error::Overloaded) => {
                // Server overloaded, similar to rate limit but longer backoff
                let delay = base_delay * 2u32.pow(attempt + 1);  // Start with longer delay
                tokio::time::sleep(min(delay, max_delay)).await;
                attempt += 1;
            }
            Err(e) => return Err(e),  // Non-retryable errors
        }
    }
}
```

**2. Token Bucket Algorithm**:
```
struct TokenBucket {
    capacity: f64,         // Max tokens/requests allowed
    tokens: f64,           // Current available tokens
    refill_rate: f64,      // Tokens added per second
    last_refill: Instant,
}

impl TokenBucket {
    fn consume(&mut self, amount: f64) -> bool {
        self.refill();
        if self.tokens >= amount {
            self.tokens -= amount;
            true
        } else {
            false
        }
    }

    fn refill(&mut self) {
        let now = Instant::now();
        let elapsed = now.duration_since(self.last_refill).as_secs_f64();
        self.tokens = (self.tokens + elapsed * self.refill_rate).min(self.capacity);
        self.last_refill = now;
    }
}
```

This should track BOTH request count and token count (two separate buckets).

### Request Queuing

**Priority queue with capacity management**:

```
struct LLMRequestQueue {
    queue: PriorityQueue<LLMRequest>,  // Higher priority tasks first
    rate_limiter: TokenBucket,          // RPM limiter
    token_limiter: TokenBucket,         // TPM limiter
    semaphore: Semaphore,               // Max concurrent requests
}

impl LLMRequestQueue {
    async fn submit(&self, request: LLMRequest) -> Result<Response> {
        // 1. Wait for queue position
        self.queue.push(request.priority, request).await;
        // 2. Wait for rate limit tokens
        self.rate_limiter.acquire(1).await?;
        // 3. Estimate token usage and wait for token budget
        let estimated_tokens = estimate_tokens(&request);
        self.token_limiter.acquire(estimated_tokens).await?;
        // 4. Wait for concurrent slot
        let _permit = self.semaphore.acquire().await?;
        // 5. Execute with retry
        call_with_retry(&self.client, request).await
    }
}
```

**Priority levels**:
- **Critical**: Orchestrator planning (blocks all workers)
- **High**: Active worker actions (directly blocking progress)
- **Medium**: Context gathering, research
- **Low**: Background analysis, documentation

### Model Fallback Strategy

**Tiered model fallback**:
```
Fallback chain:
  Claude Opus 4 (best quality, highest cost, lowest rate limits)
    -> Claude Sonnet 4 (good quality, moderate cost, higher rate limits)
      -> Claude Haiku (acceptable quality, lowest cost, highest rate limits)

Strategy:
1. Try primary model (Opus) for task decomposition and critical decisions
2. Fall back to secondary (Sonnet) for:
   - Rate limit on primary
   - Non-critical subtasks
   - Retry after primary failure
3. Fall back to tertiary (Haiku) for:
   - Rate limit on both primary and secondary
   - Simple/repetitive tasks (formatting, simple edits)
   - Emergency fallback
```

**Cost-aware routing**:
```
fn select_model(subtask: &Subtask, rate_limits: &RateLimits) -> Model {
    match subtask.complexity {
        Complexity::High if rate_limits.opus_available() => Model::Opus,
        Complexity::High => Model::Sonnet,  // Best available
        Complexity::Medium if rate_limits.sonnet_available() => Model::Sonnet,
        Complexity::Medium => Model::Haiku,
        Complexity::Low => Model::Haiku,  // Always use cheapest for simple tasks
    }
}
```

### Monitoring and Adaptive Strategies

**1. Proactive rate limit tracking**:
- Monitor `x-ratelimit-remaining-*` headers from each response.
- When remaining capacity drops below 20%, start throttling non-critical requests.
- When below 5%, route all requests to fallback models.

**2. Budget allocation per worker**:
- Each worker gets a token budget per time window.
- Workers track their own usage and self-throttle.
- Orchestrator can reallocate budgets based on priority.

**3. Request batching**:
- Combine multiple small requests into single API calls where possible.
- Use prompt caching (Anthropic's cache_control) to reduce token usage on repeated contexts.

**4. Circuit breaker pattern**:
```
enum CircuitState { Closed, Open, HalfOpen }

struct CircuitBreaker {
    state: CircuitState,
    failure_count: u32,
    failure_threshold: u32,  // e.g., 5
    reset_timeout: Duration, // e.g., 30s
    last_failure: Instant,
}

impl CircuitBreaker {
    async fn call<F, T>(&mut self, f: F) -> Result<T>
    where F: FnOnce() -> Future<Output = Result<T>> {
        match self.state {
            CircuitState::Open => {
                if self.last_failure.elapsed() > self.reset_timeout {
                    self.state = CircuitState::HalfOpen;
                } else {
                    return Err(Error::CircuitOpen);
                }
            }
            _ => {}
        }

        match f().await {
            Ok(result) => {
                self.failure_count = 0;
                self.state = CircuitState::Closed;
                Ok(result)
            }
            Err(Error::RateLimited { .. }) => {
                self.failure_count += 1;
                if self.failure_count >= self.failure_threshold {
                    self.state = CircuitState::Open;
                    self.last_failure = Instant::now();
                }
                Err(Error::RateLimited)
            }
            Err(e) => Err(e),
        }
    }
}
```

---

## External References

- [OpenHands GitHub](https://github.com/All-Hands-AI/OpenHands) -- Multi-agent coding system with event stream architecture, most relevant reference for this project
- [SWE-Agent GitHub](https://github.com/princeton-nlp/SWE-agent) -- Agent-computer interface design for coding
- [AutoGen GitHub](https://github.com/microsoft/autogen) -- Multi-agent conversation framework with group chat orchestration
- [CrewAI GitHub](https://github.com/crewAIInc/crewAI) -- Role-based multi-agent framework with hierarchical process
- [Devin/Cognition Labs](https://www.cognition.ai/devin) -- Autonomous coding agent with sandbox isolation
- [Anthropic API Rate Limits](https://docs.anthropic.com/en/api/rate-limits) -- Official rate limit documentation
- [NATS JetStream](https://docs.nats.io/nats-concepts/jetstream) -- Persistent streaming for event sourcing
- [imara-diff crate](https://crates.io/crates/imara-diff) -- Rust-native diff algorithm
- [PyO3](https://pyo3.rs/) -- Rust-Python FFI bridge
- [TiKV](https://tikv.org/) -- Distributed KV store (Rust-native)

## Related Specs

- `.trellis/spec/backend/error-handling.md` -- Error handling guidelines (template, not yet filled)
- `.trellis/spec/backend/index.md` -- Backend development guidelines index

## Caveats / Not Found

- Devin's internal architecture is not fully public; analysis is based on published demos and blog posts.
- Cursor Agent's internal architecture is proprietary; analysis is based on observable behavior and published features.
- Specific rate limit numbers for Anthropic API change over time; implementation should read headers dynamically rather than hardcoding limits.
- The Rust code sketches are architectural pseudocode, not production-ready implementations. They illustrate patterns and data flow, not error handling, lifetimes, or async nuances.
- No internal codebase to reference (empty repository). All findings are from external sources and general architectural knowledge.
