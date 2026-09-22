# Design

The Rust graph store remains the only execution authority. Add a separate Rust
read model for diagnostics, fed from task_attempts, graph_nodes and successful
execution_events in one repeatable-read, read-only transaction. Keep report
construction pure and usable without storage features. An example CLI connects
directly with a read-only-capable credential; it must not call GraphStore::connect
because that constructor runs migrations. Reports are schema-versioned JSON.

Time ratios require complete terminal-attempt timing. RUNNING attempts count
for activation only. All terminal statuses contribute measured duration; only
SUCCEEDED non-review attempts count as useful. Review duration is coordination
regardless of verdict. Neither proxy measures decomposition or gateway overhead.
Successful-event token and cost sums are explicitly partial, with independent
reported counts and no substitution of NULL with zero. Cost uses decimal text.

Explicit review is a worker result contract: append read-only and JSON format
instructions to the execution context for both single-agent and workflow paths.
Parse only a complete JSON object (optionally one JSON fence), with boolean
approved and string arrays issues/suggestions. A rejected verdict remains
observable but success=false. An execution failure or recorded file modification
cannot be approved. Existing bounded retry behavior is reused.

Capacity placement is a deterministic soft resource-allocation policy, not a
price market. The registry stores the selected policy; default is affinity.
The gateway reads UC_PLACEMENT_POLICY at startup and rejects invalid values.
Both policies use the same eligible roster; dedicated-topic availability is
still required. Compare capacity fractions by integer cross multiplication.

Alternatives: automatic optimization requires trustworthy failed-attempt usage
and objective weights; auctions require bids/budgets; auto review insertion
requires a graph mutation contract. All are deferred rather than guessed.

Known boundary: roster exclusion does not guarantee shared-queue delivery
independence. This baseline does not advertise strict independent review. A
future claim-time check or dedicated-only review policy must explicitly revise
D12/D16 before changing that guarantee.

Rollback: unset UC_PLACEMENT_POLICY; no data migration. Revert worker verdict
enforcement if deploying an older reviewer prompt, upgrading worker artifacts
together. No execution-envelope fields change.
