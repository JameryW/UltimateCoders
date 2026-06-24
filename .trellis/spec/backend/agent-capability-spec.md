# Agent Capability Spec

> Contracts for Worker self-reflection, adaptive retry, concurrent scheduling, and worker selection.

---

## 1. Scope / Trigger

This spec covers the agent capability layer in `python/ultimate_coders/agent/`:
- Worker self-evaluation (`_self_evaluate`)
- Adaptive retry with error classification (`_classify_error`, `_adaptive_retry`)
- Worker selection with capability matching (`_select_worker`)
- Concurrent subtask scheduling (`schedule_subtasks`)
- Experience recording and recall (`_record_experience`, `_gather_prior_context`)

Trigger: any change to worker execution, retry logic, scheduling, or orchestrator-worker interaction.

---

## 2. Signatures

### Worker._self_evaluate

```python
async def _self_evaluate(
    self,
    subtask: Subtask,
    summary: str,
    modified_files: list[FileChange],
    tool_log: list[dict[str, Any]] | None = None,
) -> float  # 0.0–1.0
```

Scoring table:

| Condition | Delta |
|-----------|-------|
| Baseline | +0.5 |
| Files modified | +0.2 |
| Expected output keyword overlap (proportional) | +0.0–0.2 |
| `run_command` used in tool_log | +0.1 |
| Error keywords in summary ("error", "failed", "exception", "traceback", "not found") | -0.15 |
| Empty result (no files + summary < 50 chars) | -0.1 |

Clamped to [0.0, 1.0].

### Worker._classify_error

```python
@staticmethod
def _classify_error(error: Exception) -> AdaptationStrategy
```

| Error Pattern | Strategy |
|---------------|----------|
| `asyncio.TimeoutError` or "timeout"/"timed out" in message | `SHRINK_SCOPE` |
| "no module"/"importerror" in type or "no engine"/"module" in message | `PURE_LLM` |
| "not found"/"not available" (without "engine"/"module") | `FALLBACK_TOOL` |
| "conflict"/"conflicted" in message | `WAIT_RETRY` |
| Default (unknown) | `PURE_LLM` |

### Worker._adaptive_retry

```python
async def _adaptive_retry(
    self, subtask: Subtask, error: Exception,
) -> SubtaskResult  # with .adaptation_strategy set
```

| Strategy | Behavior |
|----------|----------|
| `SHRINK_SCOPE` | Halve timeout (min 60s), `max_tokens=2048`, retry via `_execute_with_llm` |
| `PURE_LLM` | Skip all tools, call `llm_client.complete()` directly with `max_tokens=2048` |
| `FALLBACK_TOOL` | Temporarily remove codegraph tools (`symbol_search`, `find_callers`, `find_callees`, `impact_analysis`, `explore_code`), retry, restore tools |
| `WAIT_RETRY` | `asyncio.sleep(2)`, then retry via `_execute_with_llm` |

**Circuit breaker integration**: `_execute_with_llm` checks `circuit_breaker.allow_request()` before LLM call. On success → `record_success()`. On exception → `record_failure()`, then re-raise. PURE_LLM path wraps `llm_client.complete()` in try/except for the same pattern.

### Orchestrator._select_worker

```python
def _select_worker(self, subtask: Subtask) -> str | None
```

Sort: `(-capability_match_count, current_load, -max_capacity)`.

Capability matching: count how many worker capability strings appear in `subtask.description.lower()`. Workers with more matches are preferred; ties broken by lowest load.

### Orchestrator.schedule_subtasks

```python
async def schedule_subtasks(
    self, task: Task, worker_execute: Any, max_concurrent: int = 4,
) -> list[SubtaskResult]
```

**Stuck detection**: Track `completed_before` count. After each round, compute `new_progress = completed_now - completed_before`. If `new_progress == 0` for 2 consecutive rounds, break and log warning.

**Events**: Each round emits `scheduling_round_complete` with `{round_count, new_progress, total_completed, total_failed}`.

### Worker._gather_prior_context

```python
async def _gather_prior_context(self, subtask: Subtask) -> str
```

Sources (in order):
1. Dependency results: `engine.read_memory(key_scope="task", key=f"result_{dep_id}")`
2. Past experience: `engine.search_memory(query=subtask.description, scope_type="all")`, filter keys starting with `experience_`, take top 3
3. Codegraph: `self._codegraph.explore(subtask.description)`

---

## 3. Contracts

### Self-evaluate confidence thresholds

| Confidence | Action |
|------------|--------|
| < 0.5 | Mark result as `success=False` (triggers Orchestrator re-decompose) |
| 0.5–0.7 | Append `[~ confidence: X%, verify recommended]` to summary |
| ≥ 0.7 | Normal success |

### Circuit breaker behavior

| State | `_execute_with_llm` | `_adaptive_retry` |
|-------|---------------------|-------------------|
| Closed | Proceed with LLM call | N/A (not reached) |
| Open | Return `success=False` immediately with "Circuit breaker open" summary | Not invoked (execute_subtask catches the returned failure) |
| Half-open | Allow one request | N/A |

### FALLBACK_TOOL tool reduction

Removed tools (temporarily): `symbol_search`, `find_callers`, `find_callees`, `impact_analysis`, `explore_code`.

Remaining tools: `search`, `read_memory`, `write_memory`, `edit_file`, `search_memory`, `read_file`, `list_files`, `run_command`, `apply_diff`.

Tools are restored via try/finally after the adapted execution.

### Experience memory key schema

```
key_scope: "task"
key: "experience_{subtask_id}"
content: JSON {subtask_id, description, confidence, files_modified, summary}
importance: 0.6 (confidence ≥ 0.5) or 0.8 (confidence < 0.5)
```

---

## 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| No LLM client + `PURE_LLM` strategy | Return `success=False`, adaptation_strategy set |
| No engine + `_record_experience` | No-op (silently skip) |
| No engine + `_gather_prior_context` experience recall | No-op (silently skip) |
| Codegraph unavailable + `_gather_prior_context` | Skip codegraph section |
| Circuit breaker open + `_execute_with_llm` | Return `success=False` immediately |
| `tool_log=None` + `_self_evaluate` | Skip run_command check (backward compat) |
| FALLBACK_TOOL + tools restored after exception | try/finally guarantees restoration |

---

## 5. Good / Base / Bad Cases

### Self-evaluate

- **Good**: Files modified + keywords matched + run_command used → confidence ≥ 0.8
- **Base**: Files modified, no keyword match, no run_command → confidence 0.7
- **Bad**: No files + error keywords in summary → confidence < 0.5 → marked failed

### Worker selection

- **Good**: "search for auth code" → worker with `["search"]` capability selected over `["code"]`
- **Base**: "do something" (no capability match) → lowest-load worker selected
- **Bad**: No available workers → returns None, subtask skipped this round

### Scheduling

- **Good**: DAG with 3 levels, all succeed → 3 rounds, all subtasks complete
- **Base**: One subtask fails → Orchestrator auto-retries (up to max_retries)
- **Bad**: All subtasks keep failing → stuck detection triggers after 2 zero-progress rounds

---

## 6. Tests Required

| Test | Assertion Point |
|------|-----------------|
| `test_classify_error` (×7) | Each error pattern maps to correct AdaptationStrategy |
| `test_self_evaluate_high_confidence` | Files + keywords + run_command → ≥ 0.8 |
| `test_self_evaluate_error_in_summary` | Error keywords → < 0.5 |
| `test_self_evaluate_empty_result` | No files + short summary → < 0.5 |
| `test_self_evaluate_no_tool_log` | Backward compat, no crash |
| `test_record_experience_writes` | engine.write_memory called with correct key |
| `test_record_experience_no_engine` | No-op, no exception |
| `test_select_worker_capability_match` | Matching worker preferred |
| `test_select_worker_fallback_load` | No match → lowest load |
| `test_adaptive_retry_fallback_tool` | Tools reduced then restored |
| `test_adaptive_retry_shrink_scope` | Timeout halved, max_tokens reduced |
| `test_adaptive_retry_pure_llm` | No tools in call, circuit breaker tracked |
| `test_schedule_concurrent_dag` | Execution order respects dependencies |
| `test_schedule_stuck_detection` | Breaks after 2 zero-progress rounds |

---

## 7. Wrong vs Correct

### Wrong: FALLBACK_TOOL does nothing

```python
if strategy == AdaptationStrategy.FALLBACK_TOOL:
    result = await self._execute_with_llm(subtask)  # same tools!
    result.adaptation_strategy = strategy
    return result
```

### Correct: FALLBACK_TOOL actually reduces tool set

```python
if strategy == AdaptationStrategy.FALLBACK_TOOL:
    codegraph_tools = {"symbol_search", "find_callers", ...}
    original_tools = self.tools
    original_defs = self._tool_definitions
    self.tools = {k: v for k, v in self.tools.items() if k not in codegraph_tools}
    self._tool_definitions = [d for d in self._tool_definitions if d.name not in codegraph_tools]
    try:
        result = await self._execute_with_llm(subtask)
    finally:
        self.tools = original_tools
        self._tool_definitions = original_defs
    result.adaptation_strategy = strategy
    return result
```

### Wrong: _select_worker ignores capabilities

```python
candidates.sort(key=lambda w: (w.current_load, -w.max_capacity))
```

### Correct: _select_worker matches capabilities first

```python
cap_matches = {w.id: sum(1 for c in w.capabilities if c in desc_lower) for w in candidates}
candidates.sort(key=lambda w: (-cap_matches[w.id], w.current_load, -w.max_capacity))
```

### Wrong: Experience written but never read

```python
# _record_experience writes experience_*
# _gather_prior_context only reads result_*
```

### Correct: _gather_prior_context recalls experience

```python
results = self.engine.search_memory(query=subtask.description, scope_type="all", max_results=3)
experience_parts = [r.text[:200] for r in results if r.key.startswith("experience_")]
```

---

## OMP Orchestrator Extension (TypeScript)

### Scope / Trigger

- Trigger: Any change to `packages/uc-orchestrator/` — task persistence, context injection, cancel/pause/resume, gRPC bridge
- Cross-layer: OMP extension → Rust gRPC TaskService → Dashboard

### Signatures

#### TaskStore (`packages/uc-orchestrator/src/orchestrator/task-store.ts`)

```typescript
class TaskStore {
  constructor(cwd: string)
  init(): Promise<void>
  save(task: PersistedTask): Promise<void>
  load(taskId: string): Promise<PersistedTask | null>
  loadAll(): Promise<PersistedTask[]>
  loadRecoverable(): Promise<PersistedTask[]>
  remove(taskId: string): Promise<void>
}
```

#### PersistedTask

```typescript
interface PersistedTask {
  id: string
  description: string
  status: string  // "planning" | "in_progress" | "completed" | "failed" | "cancelled"
  error?: string
  controlState: "running" | "paused" | "cancelled"
  subtasks: Array<{
    id: string; description: string; status: string; dependsOn: string[]
    result?: string; error?: string
    review?: { approved: boolean; issues: string[]; suggestions: string[] }
    startedAt?: number; completedAt?: number
  }>
  createdAt: number; completedAt?: number
}
```

#### Orchestrator Control Methods

```typescript
class UCOrchestrator {
  cancelTask(taskId: string, subtaskId?: string, ctx?: ExtensionCommandContext): Promise<boolean>
  pauseTask(taskId: string, ctx?: ExtensionCommandContext): Promise<boolean>
  resumeTask(taskId: string, ctx: ExtensionCommandContext): Promise<boolean>
  restore(): Promise<void>  // Recover persisted tasks on startup
}
```

#### GrpcBridge Extensions

```typescript
class GrpcBridge {
  upsertTask(task: PersistedTask): Promise<boolean>
  pauseTask(taskId: string): Promise<boolean>
  resumeTask(taskId: string): Promise<boolean>
}
```

### Contracts

#### Persistence Strategy (Dual-Write)

| Priority | Storage | Purpose | Failure Mode |
|----------|---------|---------|-------------|
| 1 (truth) | `.uc/tasks/<id>.json` | Local persistence, restart recovery | No failure (local fs) |
| 2 (view) | gRPC TaskService | Dashboard visibility | Best-effort, fire-and-forget |

- Every state change: `persist(task)` first, then `syncTaskToGrpc(task)` async
- `syncTaskToGrpc` uses `upsertTask` which checks existence via `getTask()` before submitting
- gRPC failures are non-fatal (catch + ignore)

#### Context Injection (500-char limit)

- `buildContextForSubtask(def, task)` injects summaries of completed prerequisite subtasks
- Total summary capped at 500 chars (accumulative tracking, truncated)
- Each subtask result sliced to 200 chars within summary
- Worker results auto-written to uc_memory: `scope="task", key="subtask_result_<id>"`

#### Cancel/Pause/Resume

| Command | Behavior | State Validation |
|---------|----------|-----------------|
| `/uc cancel <task-id>` | Abort running subtasks, mark task cancelled | Any status → cancelled |
| `/uc cancel <task-id> <subtask-id>` | Cancel subtask + cascade downstream, task continues | Subtask must exist |
| `/uc pause <task-id>` | Finish current wave, mark paused | Only in_progress/planning |
| `/uc resume <task-id>` | Rebuild DAG from pending subtasks, continue | Only paused |

- Cascade cancel: if subtask X is cancelled, all subtasks depending on X are also cancelled
- Subtask-level cancel does NOT cancel the whole task — remaining non-dependent paths continue
- Pause: `resumeFromWave` tracks wave index; `resumeTask` rebuilds DAG from pending subtasks
- `AbortController` per task: cancel aborts all running worker subprocesses

#### Extension Commands

| Command | Arguments | Description |
|---------|-----------|-------------|
| `/uc submit` | `<description>` | Submit task for orchestration |
| `/uc status` | `[task-id]` | Show task status |
| `/uc cancel` | `<task-id> [<subtask-id>]` | Cancel task or subtask |
| `/uc pause` | `<task-id>` | Pause after current wave |
| `/uc resume` | `<task-id>` | Resume paused task |
| `/uc help` | — | Show help |

### Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| No `.uc/tasks/` directory | `TaskStore.init()` creates it via `mkdir -p` |
| Corrupt JSON in task file | `TaskStore.load()` returns null (try/catch) |
| gRPC server unavailable | All bridge methods return defaults (null/[]/false) |
| Cancel non-existent task | `cancelTask` returns false |
| Pause completed task | `pauseTask` returns false (wrong status) |
| Resume non-paused task | `resumeTask` returns false |
| Resume with no pending subtasks | Task marked completed immediately |
| All subtasks cancelled (subtask-level) | Task continues, remaining paths execute |

### Good/Base/Bad Cases

- **Good**: Submit → decompose → execute waves → all complete → persist + sync
- **Base**: Submit → wave 2 fails → task failed → persist + sync
- **Bad**: Submit → process crashes mid-wave → restart → `restore()` recovers in_progress task → resume

### Tests Required

| Test | Assertion |
|------|-----------|
| `task_store_save_load` | Round-trip: save then load returns same task |
| `task_store_load_nonexistent` | Returns null |
| `task_store_load_all` | Multiple saves → loadAll returns all |
| `task_store_recoverable` | Only planning/in_progress/paused returned |
| `task_store_recoverable_excludes_cancelled` | Cancelled tasks excluded |
| `task_store_empty_dir` | Empty directory returns [] |
| `task_store_overwrite` | Save same ID twice → last write wins |
| `task_store_subtask_data` | Result, review, timestamps survive round-trip |

### Common Mistakes

1. **Casting `pi.zod.object({...})` directly into `registerTool` parameters** — causes TS2589 "Type instantiation is excessively deep". Extract schema as variable and use `as never` cast, type params manually in execute callback.

2. **Subtask-level cancel cancelling the whole task** — only cascade downstream subtasks; the parent task should continue executing remaining non-dependent paths.

3. **Forgetting `syncTaskToGrpc` after control operations** — cancel/pause/resume must sync state to gRPC for dashboard visibility.

4. **`runningCount` not decremented on early return** — if a worker exits early (e.g., cancel), must decrement `runningCount` to prevent counter leak.

5. **Context summary exceeding token budget** — `buildContextForSubtask` must cap total at 500 chars to avoid token overflow in worker prompts.
