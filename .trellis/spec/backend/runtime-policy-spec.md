# Runtime policy baseline

Executable contracts for the P2 baseline: a read-only runtime report, the
explicit `review` verdict, and opt-in capacity placement. Narrative and
rollback live in `docs/architecture/durable-runtime-p2-policy.md`. Dispatch
gates stay in [`worker-service-spec.md`](./worker-service-spec.md). The
`review` opt-in stays in [`agent-capability-spec.md`](./agent-capability-spec.md).

---

## Scenario: read-only runtime report

### 1. Scope / Trigger

- Trigger: reading one durable graph's attempt and usage history.
- The report never migrates, writes, or calls `GraphStore::connect`.

### 2. Signatures

```text
cargo run -p uc-engine --no-default-features --features storage --example runtime_report -- GRAPH_ID
```

```rust
pub async fn read_runtime_report(pool: &sqlx::PgPool, graph_id: &str) -> Result<RuntimeReport, EngineError>
```

`RuntimeReport` is schema version `1`: `useful_work_ratio`, `coordination_ratio`,
`activation_inflation` (`Option<f64>`), `recommendations` (`Vec<&'static str>`),
and `metrics`.

### 3. Contracts

- `UC_DATABASE_URL` is required. One non-empty `GRAPH_ID` argument.
- One transaction: `REPEATABLE READ, READ ONLY`. Both statements bind `graph_id`.
- Terminal statuses: `SUCCEEDED`, `FAILED`, `CANCELLED`, `SKIPPED`.
- Useful milliseconds: `SUCCEEDED` and `graph_nodes.type <> 'review'`, with
  `finished_at >= started_at`.
- Coordination milliseconds: every terminal `type = 'review'` attempt, any verdict.
- Activation inflation: all persisted attempts / distinct attempted nodes.
  `RUNNING` attempts count here only.
- Time ratios are null unless every terminal attempt has a usable duration and
  the summed duration is positive. Empty activation is null.
- `reported_tokens` / `reported_cost_usd` sum `execution_events` columns on
  `event_type = 'node_succeeded'` only. `payload.steps` is not read. Cost is
  decimal text. NULL sum stays null; a measured zero stays zero.
- Recommendations, only when true: `collect_missing_attempt_timings`,
  `complete_successful_usage_reporting`, `inspect_repeated_activations`.

### 4. Validation & Error Matrix

| Condition | Result |
| --- | --- |
| Missing or extra CLI args, blank graph id, unset `UC_DATABASE_URL` | usage error, exit non-zero |
| Built without `storage` | stderr + exit 2 |
| Graph id absent | `EngineError::NotFound` |
| Database or schema failure | `EngineError::StorageError` |
| Reversed or missing terminal timestamps | that attempt is untimed; both time ratios null |

### 5. Good/Base/Bad Cases

- Good: 60ms successful build + 20ms failed build retry + 20ms review → useful `0.6`, coordination `0.2`, activation `1.5`.
- Base: no attempts → all three ratios null, no fabricated zero.
- Bad: one terminal attempt missing timing, or a `node_succeeded` row with NULL tokens → ratio or sum stays null and a recommendation is present.

### 6. Tests Required

| Test | Assertion |
| --- | --- |
| `runtime_metrics::worked_graph_exposes_ratios_and_partial_measurements` | `0.6` / `0.2` / `1.5`; absent usage is null |
| `empty_and_incompletely_timed_graphs_have_no_time_ratios` | partial timing nulls both ratios; activation still computed |
| `measured_zero_is_distinct_from_absent_usage` | zero tokens/cost stay present; JSON nulls absent fields |
| `postgres_runtime_report_uses_one_graph_and_never_counts_step_usage_twice` | ignored live PG: step payload `999` is not added; absent graph is `NotFound`; clearing one timestamp nulls ratios |

### 7. Wrong vs Correct

#### Wrong

Call `GraphStore::connect` from the report CLI, or treat NULL usage as `0`.

#### Correct

Open a plain pool, read inside one read-only snapshot, and keep unknown sums as JSON null.

---

## Scenario: explicit review verdict

### 1. Scope / Trigger

- Trigger: `required_capabilities` contains the exact string `review`.
- `code-review`, `Review`, and the word "review" in a description do not enter this contract.

### 2. Signatures

```python
REVIEW_INSTRUCTIONS: str
def parse_review(summary: str) -> SubtaskReview | None
```

`SubtaskReview`: `approved: bool`, `issues: list[str]`, `suggestions: list[str]`.

### 3. Contracts

- Single-agent execution prepends `REVIEW_INSTRUCTIONS` to the prompt.
- A workflow step receives the same block. A template that already expands
  `{{context}}` is not given a second copy.
- Accepted text is one JSON object, or one fence that is exactly a leading
  `` ```json\n `` and a trailing `` \n``` ``. `approved` is a boolean. `issues`
  and `suggestions` are arrays of strings. Extra prose is rejected.
- A rejected verdict stays on `SubtaskResult.review` with `success=false`.
- Invalid JSON, executor failure, `file_changes`, `failed_file_changes`, or
  `had_failed_step` cannot succeed and do not keep a verdict.
- An ordinary workflow still succeeds when its last step succeeds after a
  non-aborting earlier failure. `had_failed_step` records that failure; it does
  not flip ordinary `success`.
- Checkpoint replay of a review requires `success`, an approved verdict, and no
  `modified_files`. Anything else re-executes.

### 4. Validation & Error Matrix

| Condition | `success` | `review` | `error` |
| --- | --- | --- | --- |
| `approved: true`, no file changes, executor success | true | approved | empty |
| `approved: false` with a valid object | false | rejected object kept | `Review rejected: …` |
| Fence/object malformed, wrong types, or prose | false | None | `Review returned no valid JSON verdict` |
| Executor `success=false` | false | None | summary tail |
| Any recorded file change | false | None | `Review modified files` |
| Earlier non-aborting step failed | false | None | `Review workflow contained a failed step` or `Review modified files` |

### 5. Good/Base/Bad Cases

- Good: workflow step prompt `Inspect this change` still contains the read-only JSON contract, and a valid approval is returned.
- Base: capability `code` with summary `done` succeeds and `review is None`.
- Bad: approval JSON plus a file change, or a checkpoint without `approved: true`, is not replayed as success.

### 6. Tests Required

| Test | Assertion |
| --- | --- |
| `test_explicit_rejection_is_not_a_successful_execution` | `success is False` and `review.approved is False` |
| `test_review_requires_a_valid_successful_read_only_verdict` | malformed, file-changing, and failed executions have `review is None`; the prompt contains the contract |
| `test_normal_nodes_do_not_require_a_json_verdict` | `code`, `code-review`, `Review` stay successful prose |
| `test_workflow_review_uses_the_same_verdict_contract` | verdict parsed; instructions present without `{{context}}` |
| `test_review_cannot_hide_a_non_aborting_failed_step` | failed step cannot be approved |
| `test_checkpoint_replay_preserves_approved_verdict` | second call does not execute again |
| `test_execute_steps_continues_when_abort_on_failure_false` | ordinary `success is True` and `had_failed_step is True` |

### 7. Wrong vs Correct

#### Wrong

Treat any capability or description containing "review" as this contract, or fail an ordinary non-aborting workflow because an earlier step failed.

#### Correct

Match the exact `review` capability, require the JSON object there, and leave ordinary last-step success unchanged.

---

## Scenario: capacity placement

### 1. Scope / Trigger

- Trigger: gateway startup reads `UC_PLACEMENT_POLICY` before backends start.
- Default remains affinity. Capacity does not add a delivery protocol.

### 2. Signatures

```rust
pub enum PlacementPolicy { Affinity, Capacity } // FromStr: "affinity" | "capacity"
pub fn place_with_policy(...) -> Option<Placement>
impl WorkerRegistry { pub fn set_placement_policy(&mut self, policy: PlacementPolicy) }
```

`placement_target` passes the registry's stored policy into `place_with_policy`.

### 3. Contracts

- Unset `UC_PLACEMENT_POLICY` means `affinity`. Any other string, including
  `""` or `Capacity`, is `EngineError::ConfigError` and the process returns
  before storage or subscribers start.
- Both policies use `dispatch_candidates`: capability, scope, contract version,
  availability, and producer exclusion, then require `per_worker_topic`.
- Affinity order: hits desc, load percent asc, same host, worker id asc.
  Zero hits returns `None` (shared subject).
- Capacity order: exact `current_load / max_capacity` via `u64` cross
  multiplication, then hits desc, same host, worker id asc. Zero hits are
  eligible. `max_capacity == 0` or `current_load >= max_capacity` is skipped.
- No dedicated candidate still publishes to the shared subject.
- Roster exclusion is still not a shared-queue delivery guarantee.

### 4. Validation & Error Matrix

| Condition | Result |
| --- | --- |
| Unset / `affinity` | existing affinity placement |
| `capacity` | lowest exact load fraction, then affinity |
| `auction`, blank, or different case | startup `ConfigError` |
| Eligible reviewer removed, producer excluded | `placement_target` is `None` |
| Equal load fraction | higher affinity, then same host, then ascending worker id |

### 5. Good/Base/Bad Cases

- Good: capacity selects idle worker `idle` (0 hits) over affine worker `busy`.
- Base: `place` without a policy still selects `busy`.
- Bad: two full workers, or two zero-capacity workers, return `None`.

### 6. Tests Required

| Test | Assertion |
| --- | --- |
| `capacity_policy_can_prefer_idle_worker_without_affinity` | capacity picks `idle`; default `place` picks `busy` |
| `capacity_policy_compares_exact_fractions_and_wire_extremes` | `1/999` beats `2/999`; `u32::MAX` load percent does not overflow the rank |
| `capacity_policy_ties_are_stable_and_unavailable_workers_overflow` | tie breaks to `a`; full and zero capacity return `None`; `auction` does not parse |
| `capacity_placement_preserves_eligibility_and_review_exclusion` | wrong cap, scope, version, legacy topic, and the producer are not selected |

### 7. Wrong vs Correct

#### Wrong

Rank capacity with rounded `load_percent`, or let `UC_PLACEMENT_POLICY=capacity` drop the producer-exclusion gate.

#### Correct

Compare fractions by cross multiplication on the same roster `dispatch_candidates` already filtered.
