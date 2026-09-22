# P2 runtime policy baseline

This repository-grounded design follows the user's 2026-09-20 request to
continue design and implementation. It does not reconstruct the missing
external proposal. Map #656 remains the umbrella. This baseline delivers
diagnostics, explicit verdict handling and opt-in capacity placement.

## Ownership

Rust remains the only graph/attempt authority. Diagnostics read its persisted
history; recommendations do not mutate execution. Python emits the existing
SubtaskReview contract for explicit review nodes. Placement chooses an initial
dedicated subject from the existing eligible roster, preserving queue semantics.

## Optimizer diagnostics, schema version 1

Use the gateway's existing database (set UC_DATABASE_URL through your normal
credential mechanism):

```powershell
cargo run -p uc-engine --no-default-features --features storage --example runtime_report -- GRAPH_ID
```

Output is JSON. Missing configuration/graph, unavailable database or incompatible
schema fails explicitly. No migrations run. Reads share a read-only repeatable-
read snapshot and bind the graph ID. A SELECT-only account can be used.

| Metric | Definition |
| --- | --- |
| Useful work ratio | Successful non-review terminal attempt milliseconds / all terminal attempt milliseconds |
| Coordination ratio | Review terminal attempt milliseconds / all terminal attempt milliseconds |
| Activation inflation | All persisted attempts / distinct nodes with an attempt |

Terminal means SUCCEEDED, FAILED, CANCELLED or SKIPPED (the latter two occur in
imported history). Missing or reversed start/end timestamps are unknown. Both
time ratios are null unless every terminal attempt has usable timing and the
sum is positive. Zero durations are measured values but cannot be denominators.
An empty activation population returns null. Concurrent durations represent
summed effort, not wall-clock latency. Imported history may omit earlier retries.

These are operational proxies, not quality measures: success need not mean
useful product output, and review time excludes gateway/decomposition overhead.
Recommendations identify missing measurements and repeated activations, without
automatically tuning the system.

`metrics.reported_tokens` and `metrics.reported_cost_usd` sum reported
node_succeeded events only. Independent reported-event counts accompany the
successful-event denominator. Unknown sums remain null; measured zero remains
zero. Cost is decimal text. Failed/running usage is unavailable from these
columns. `payload.steps` is excluded to avoid double-counting overlapping usage.
Multi-step node totals can represent only the final adapter: these fields are
not an invoice or whole-run cost estimate.

## Explicit review

Use the existing exact `review` capability and dependency edges to producer
nodes. Reviewer workers opt in with UC_CAP_REVIEW. For both single-agent and
workflow execution, the worker adds read-only and output-format instructions.
The final response must be a complete JSON object:

```json
{"approved": false, "issues": ["Missing error-path test"], "suggestions": []}
```

A single `json` Markdown fence is accepted. Prose outside JSON, non-boolean
approval, absent arrays or non-string array members are invalid. Rejected
verdicts remain on the result with success=false. Invalid output, executor
failure or reported file modifications cannot be approved. Attempt checkpoints
preserve valid approvals; older review checkpoints lacking them are re-executed.
Ordinary tasks, including `code-review` and `Review`, retain their prior behavior.

The prompt and reported-change checks are not an OS security boundary. Configure
reviewers with deployment-appropriate read-only tools. Existing worker/gateway
retry budgets apply; this version does not insert review nodes, generate repairs,
or reset committed producers.

T19 roster exclusion is not a delivery guarantee: shared fallback can still
deliver to a producer when another reviewer exists. Strict independent execution
is not claimed. Claim-time rejection or dedicated-only review requires a separate
D12/D16 revision.

## Capacity placement

Set UC_PLACEMENT_POLICY=capacity on the standalone gateway before startup.
Unset it (or use affinity) for the default. Invalid values fail startup. The
programmatic WorkerRegistry API also has an explicit policy setter.

Both modes preserve capability, project scope, contract version, heartbeat,
capacity and producer-exclusion checks and require an advertised dedicated
subject. Capacity mode ranks by exact current_load / max_capacity, then greatest
file affinity, sibling-host locality, and ascending worker ID. Zero file overlap
is eligible. If no dedicated candidate qualifies, existing shared fallback is
retained. This explicitly revises D12's zero-affinity preference only when opted
in. Default affinity behavior is preserved.

Loads may lag until heartbeat. This is not a reservation or fairness guarantee.
The bounded replacement for the undefined market idea is capacity allocation;
monetary bids, auctions, tenancy quotas and cost-based ranking are not implemented.

## Remaining boundaries and rollback

Adaptive optimization needs an objective and failed-attempt usage. Strict review
independence needs admission/delivery enforcement. Repair loops need versioned
graph expansion. A monetary market needs bids and budgets. These are separate
follow-up decisions, not completed by this baseline.

No migration is needed to roll back. Unset the placement setting, stop consuming
the report, or revert worker verdict enforcement. Deploy review output expectations
together with worker code. No execution-envelope field changes are introduced.
