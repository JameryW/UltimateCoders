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
| `test_adaptive_retry_pure_llm` | No tools in call |
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

---

## Sandbox Agent Customization (Skill / MCP / Tool)

### Scope / Trigger

Worker sandbox agents (claude-code, codex) can now be customized with tool restrictions, MCP servers, system prompts, and custom agent definitions. Trigger: any change to `SandboxConfig` agent fields, `Subtask.agent_config`, `ClaudeCodeAdapter.build_request`, or `Worker._derive_capabilities`.

### Signatures

#### SandboxConfig — new agent fields

```python
@dataclass
class SandboxConfig:
    # ... existing fields ...
    tools: list[str] | None = None              # --tools
    allowed_tools: list[str] | None = None      # --allowedTools
    disallowed_tools: list[str] | None = None   # --disallowedTools
    mcp_configs: list[str] | None = None        # --mcp-config
    append_system_prompt: str | None = None      # --append-system-prompt
    agent_name: str | None = None                # --agent
    agents_json: str | None = None               # --agents
```

#### Subtask.agent_config

```python
@dataclass
class Subtask:
    # ... existing fields ...
    agent_config: dict[str, Any] = field(default_factory=dict)
    # Keys: tools, allowed_tools, disallowed_tools, mcp_configs,
    #       append_system_prompt, agent_name, agents_json
```

#### _merge_agent_config

```python
def _merge_agent_config(
    config: SandboxConfig,
    subtask_config: dict[str, Any] | None = None,
) -> dict[str, Any]
```

Merges SandboxConfig agent fields with per-subtask overrides. Subtask-level values replace (not merge) config-level values for the same key.

#### Worker._derive_capabilities

```python
def _derive_capabilities(self) -> list[str]
```

Base: `["code", "search", "memory", "test", "decompose", "review"]`. Enhanced:
- `mcp_configs` → `"mcp"` + per-server `"mcp:<server>"` (extracted from dict keys or file path basename)
- `tools` with `"mcp__<server>__*"` pattern → `"mcp:<server>"` per prefix
- `agent_name` → `"agent:<name>"`
- `agents_json` → parsed for agent names → `"agent:<name>"` each
- Deduplicated (preserving order)

#### Worker._resolve_agent_config

```python
def _resolve_agent_config(self, subtask: Subtask) -> dict[str, Any]
```

Priority: explicit > capability match > description heuristic.
1. `subtask.agent_config` non-empty → use as-is (user explicit override)
2. `subtask.required_capabilities` → match against `AGENT_PROFILES` dict
3. Description keywords → match against `SUBTASK_TEMPLATES` dict (template fills only keys not already set by capability match)

#### AGENT_PROFILES / SUBTASK_TEMPLATES

```python
AGENT_PROFILES = {
    "review": {"disallowed_tools": ["Edit", "Write", "NotebookEdit"], ...},
    "codegraph": {"tools": ["default", "mcp__codegraph__*"]},
    "code": {"tools": ["default"]},
}
SUBTASK_TEMPLATES = {
    "review": {"disallowed_tools": [...], ...},
    "search": {"tools": ["default", "mcp__codegraph__*"]},
}
```

#### _resolve_mcp_configs

```python
def _resolve_mcp_configs(
    mcp_configs: list[str | dict[str, Any]],
) -> tuple[list[str], list[str]]
```

Resolves MCP configs: file paths pass through, inline JSON dicts write to temp `.mcp.json` files. Returns `(resolved_paths, temp_file_paths)` — caller must clean up temp files.

#### _codex_mcp_server_toml

```python
def _codex_mcp_server_toml(name: str, cfg: dict[str, Any]) -> str
```

Converts a single MCP server config dict to Codex config.toml `[mcp_servers."name"]` section. Handles stdio (command/args/env) and streamable-http (url/bearer_token_env_var) transports. All string values are TOML-escaped via `_toml_escape()` to prevent injection.

### Contracts

#### CLI flag mapping

**Claude Code adapter** — direct CLI flags:

| Config key | CLI flag | Example |
|------------|----------|---------|
| `tools` | `--tools` | `["default", "mcp__codegraph__*"]` |
| `allowed_tools` | `--allowedTools` | `["Bash(git *)", "Edit"]` |
| `disallowed_tools` | `--disallowedTools` | `["Bash(rm *)"]` |
| `mcp_configs` | `--mcp-config` | `["/etc/mcp/codegraph.json"]` or inline `{"codegraph": {...}}` |
| `append_system_prompt` | `--append-system-prompt` | `"Focus on Rust code"` |
| `agent_name` | `--agent` | `"reviewer"` |
| `agents_json` | `--agents` | `'{"reviewer": {...}}'` |

**Codex adapter** — config.toml driven (NOT CLI flags):

| Config key | config.toml target | Notes |
|------------|-------------------|-------|
| `mcp_configs` | `[mcp_servers."name"]` section | Writes temp `.config.toml` in `$CODEX_HOME/`, uses `--profile <stem>` |
| `allowed_tools` | Per-server `enabled_tools` | Codex has no global allow/deny; per-server only |
| `disallowed_tools` | Per-server `disabled_tools` | Same limitation |
| Other keys | Not supported via config.toml | `--sandbox workspace-write` replaces deprecated `--full-auto` |

> **Key difference**: Claude Code extends tools via CLI flags at invocation time. Codex extends tools via `config.toml` written before execution. The Python `CodexAdapter` bridges this by writing a temporary `.config.toml` and passing `--profile <stem-name>`.

#### Inline MCP config handling

`mcp_configs` entries can be:
- **File path** (string): passed as-is to `--mcp-config` (Claude) or read + embedded in config.toml (Codex)
- **Inline dict** (dict): written to temp `.mcp.json` file (Claude) or embedded directly in config.toml `[mcp_servers]` section (Codex)

Temp files are created in `SandboxManager.execute`, tracked in `_temp_files` list, and cleaned up in the `finally` block.

#### Override precedence

1. `Subtask.agent_config` non-empty → use as-is (user explicit override, bypasses derivation)
2. `Subtask.required_capabilities` → match `AGENT_PROFILES` (e.g., `"review"` → `disallowed_tools`)
3. Description heuristics → match `SUBTASK_TEMPLATES` (e.g., "review" keyword → review template)
4. `SandboxConfig` fields → baseline values when no subtask-level override exists

List semantics: lists are **replaced**, not merged — subtask config defines the full set.

#### Execution flow

```
Worker._execute_in_sandbox(subtask)
  → agent_config = Worker._resolve_agent_config(subtask)  # auto-derive or use explicit
  → SandboxManager.execute(prompt, subtask_config=agent_config)
    → Adapter.build_request(prompt, wd, config, subtask_config)
      → _merge_agent_config(config, subtask_config)  # merge config-level + subtask-level
      → _resolve_mcp_configs(merged["mcp_configs"])  # inline JSON → temp files
      → generates CLI args (Claude) or config.toml (Codex)
    → SandboxManager.execute finally: clean up _temp_files
```

#### Backward compatibility

All new fields default to `None` / `{}`. When all are unset, `ClaudeCodeAdapter.build_request` generates the same CLI args as before.

### Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| `tools=[]` | `--tools` flag emitted with empty list — claude CLI may reject. Prefer `None` for "no override". |
| `mcp_configs` path not found | claude CLI fails with error — worker returns `SubtaskResult(success=False)` |
| `agents_json` invalid JSON | claude CLI fails with error — worker returns `SubtaskResult(success=False)` |
| Both `SandboxConfig.tools` and `Subtask.agent_config["tools"]` set | Subtask value wins (replace, not merge) |
| `agent_config={}` (empty) | No override, config-level values used |

### Good / Base / Bad Cases

- **Good**: Subtask with `agent_config={"tools": ["mcp__codegraph__*"], "mcp_configs": ["/mcp/codegraph.json"]}` → worker gets codegraph tools for that subtask only
- **Base**: No agent_config → default claude-code behavior (all tools, no MCP)
- **Bad**: `mcp_configs` pointing to missing file → claude CLI error → subtask fails

### Tests Required

| Test | Assertion Point |
|------|-----------------|
| `test_default_agent_fields_are_none` | All new SandboxConfig fields default to None |
| `test_custom_agent_fields` | All fields set correctly |
| `test_merge_config_fields_only` | Config fields extracted |
| `test_merge_subtask_overrides` | Subtask value replaces config value |
| `test_merge_subtask_adds_new_key` | New key from subtask added |
| `test_no_extra_flags_by_default` | No extra CLI args when all fields None |
| `test_tools_flag` | `--tools` with correct values in args |
| `test_mcp_config_flag` | `--mcp-config` with paths in args |
| `test_allowed_tools_flag` | `--allowedTools` with patterns in args |
| `test_disallowed_tools_flag` | `--disallowedTools` with patterns in args |
| `test_append_system_prompt_flag` | `--append-system-prompt` with text in args |
| `test_agent_name_flag` | `--agent` with name in args |
| `test_agents_json_flag` | `--agents` with JSON in args |
| `test_subtask_config_overrides` | Subtask config takes precedence in build_request |
| `test_all_flags_together` | All flags present in args |
| `test_default_capabilities` | Base caps: code, search, memory, test |
| `test_mcp_capability_when_mcp_configs_set` | "mcp" added |
| `test_codegraph_capability_when_tool_present` | "codegraph" added |
| `test_agent_name_capability` | "agent:<name>" added |
| `test_explicit_capabilities_override_derived` | Explicit list overrides derivation |
| `test_subtask_agent_config_round_trip` | to_dict/from_dict preserves agent_config |
| `test_resolve_mcp_configs_file_paths` | File paths pass through unchanged |
| `test_resolve_mcp_configs_inline_dict` | Inline dict → temp file with correct JSON |
| `test_codex_mcp_server_toml_stdio` | Stdio transport generates correct TOML |
| `test_codex_mcp_server_toml_http` | HTTP transport generates correct TOML |
| `test_codex_adapter_profile_uses_stem` | --profile uses stem name, not full path |
| `test_claude_code_inline_mcp` | Inline MCP dict → temp .mcp.json file |
| `test_worker_derive_per_server_caps` | mcp_configs → mcp:<server> capabilities |
| `test_worker_derive_agents_json` | agents_json → agent:<name> capabilities |
| `test_worker_resolve_explicit_preserved` | Explicit agent_config bypasses derivation |
| `test_worker_resolve_capability_match` | required_capabilities → AGENT_PROFILES match |
| `test_worker_resolve_description_template` | Description keywords → SUBTASK_TEMPLATES match |
| `test_agent_config_pipeline_review` | End-to-end: review → disallowedTools in CLI args |
| `test_temp_files_cleaned_up` | SandboxManager.execute cleans _temp_files |

### Wrong vs Correct

#### Wrong: Merging lists instead of replacing

```python
# If config has ["default"] and subtask has ["mcp__codegraph__*"],
# you'd get ["default", "mcp__codegraph__*"] — subtask can't narrow tools
merged_tools = (config.tools or []) + (subtask_config.get("tools") or [])
```

#### Correct: Subtask replaces config list

```python
result["tools"] = subtask_config["tools"]  # full replacement
```

#### Wrong: Hardcoded capabilities ignore config

```python
self.capabilities = capabilities or ["code", "search", "memory", "test"]
```

#### Correct: Derive from config

```python
self.capabilities = capabilities or self._derive_capabilities()
```

#### Wrong: Codex --profile with full file path

```python
# Codex --profile expects a NAME, not a path.
# It looks up $CODEX_HOME/<name>.config.toml
fd, config_path = tempfile.mkstemp(suffix=".toml", prefix="uc-codex-")
args += ["--profile", config_path]  # WRONG: full path like /tmp/uc-codex-XXXX.toml
```

#### Correct: Codex --profile with stem name

```python
# Write to $CODEX_HOME/ with .config.toml suffix (Codex convention)
# Pass only the stem (filename without .config.toml) to --profile
fd, config_path = tempfile.mkstemp(suffix=".config.toml", prefix="uc-codex-", dir=codex_home)
profile_name = os.path.basename(config_path).removesuffix(".config.toml")
args += ["--profile", profile_name]  # e.g. "uc-codex-XXXX"
```

#### Wrong: TOML injection via unescaped values

```python
# MCP server name or config values with special chars break TOML parsing
lines.append(f"[mcp_servers.{name}]")  # breaks if name contains dots
lines.append(f'command = "{cfg["command"]}"')  # breaks if value contains quotes
```

#### Correct: TOML-escape all values and quote table names

```python
lines.append(f'[mcp_servers."{_toml_escape(name)}"]')
lines.append(f'command = "{_toml_escape(cfg["command"])}"')
```
