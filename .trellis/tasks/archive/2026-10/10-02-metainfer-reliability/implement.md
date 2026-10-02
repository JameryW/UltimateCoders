# Implementation

Baseline: `7d6bfbfa`; branch: `codex/metainfer-reliability`.

The Worker now binds strict worktree leases, execution checkpoints and inference
experiments to the same transactional runtime store. PostgreSQL is shared across
the deployed services; SQLite remains the local fallback. A runner registers its
process birth under the lease lock before executing, so a delayed child and a
replacement Worker cannot acquire the same worktree concurrently.

Remote submissions persist intent before POST and remote identity before polling.
Known jobs resume; uncertain submission/termination quarantines the candidate and
budget. Atomic iteration checkpoints retain the original baseline and transaction.
UC commits accepted edits, records delivery before removing the worktree, and can
recover delivery without rerunning the adapter.

Terminal outcomes enter an immutable outbox before dispatch ACK. Coordinator
recovery hydrates the Gateway's current attempt and waits for durable task/event
confirmation. Full snapshots fence stale/future attempts and preserve control
states; partial same-attempt results cannot revive cancelled nodes. Per-node
cancellation retains every duplicate execution, including coroutines waiting for
capacity. Rust timestamps are normalized for Python 3.9; Python plan verification
commands and project/user constraints survive hydration.
Current-dispatch verification commands live in the coordinator plan record;
generic task checkpoint restore retains its #561 exclusion rule.

Readiness is operation-specific and refreshed asynchronously. Local benchmarks
remain available without MetaInfer. Repeated measurements carry dispersion and
environment identity; hard ceilings cover every sample. Authenticated artifact
queries validate manifest integrity and exclude rollback checkpoints.

Deployment wiring, README, architecture/domain documents and executable specs
were updated. Final evidence and external-GPU boundaries are recorded in
`docs/metainfer-reliability-verification.md`.

## Commit plan

One local commit: `fix(inference): make MetaInfer execution and delivery restart-safe`.

Files owned by this repair (57):

```text
.scratch/metainfer-reliability/issues/01-runtime-reliability.md
.scratch/metainfer-reliability/spec.md
.trellis/spec/backend/agent-capability-spec.md
.trellis/spec/backend/database-guidelines.md
.trellis/spec/backend/inference-infra-spec.md
.trellis/spec/backend/nats-bridge-spec.md
.trellis/tasks/10-02-metainfer-reliability/check.jsonl
.trellis/tasks/10-02-metainfer-reliability/implement.jsonl
.trellis/tasks/10-02-metainfer-reliability/implement.md
.trellis/tasks/10-02-metainfer-reliability/prd.md
.trellis/tasks/10-02-metainfer-reliability/review.md
.trellis/tasks/10-02-metainfer-reliability/task.json
README.md
README.zh-CN.md
crates/uc-engine/src/task_store.rs
crates/uc-grpc/src/server.rs
docker/docker-compose.yml
docs/architecture.md
docs/inference-infra.md
docs/metainfer-reliability-verification.md
pyproject.toml
python/ultimate_coders/agent/harness_metainfer.py
python/ultimate_coders/agent/orchestrator.py
python/ultimate_coders/agent/sandbox.py
python/ultimate_coders/agent/types.py
python/ultimate_coders/agent/worker.py
python/ultimate_coders/agent/workspace.py
python/ultimate_coders/dashboard/app.py
python/ultimate_coders/inference/adapter.py
python/ultimate_coders/inference/agent.py
python/ultimate_coders/inference/artifacts.py
python/ultimate_coders/inference/benchmark.py
python/ultimate_coders/inference/models.py
python/ultimate_coders/inference/oracle.py
python/ultimate_coders/inference/runner.py
python/ultimate_coders/inference/workflow.py
python/ultimate_coders/nats_worker.py
python/ultimate_coders/runtime_state.py
scripts/check-spec-refs.py
tests/python/conftest.py
tests/python/test_affinity_placement.py
tests/python/test_check_line_endings.py
tests/python/test_check_spec_refs.py
tests/python/test_check_tasks_refs.py
tests/python/test_cooperative_cancel.py
tests/python/test_execution_scope.py
tests/python/test_inference_delivery.py
tests/python/test_inference_oracle.py
tests/python/test_inference_reliability.py
tests/python/test_inference_routing.py
tests/python/test_inference_runtime_live.py
tests/python/test_nats_jetstream_subtask.py
tests/python/test_nats_worker_helpers.py
tests/python/test_night_window_exclusive.py
tests/python/test_runtime_state.py
tests/python/test_sandbox.py
tests/python/test_workspace.py
```

Unrecognized dirty files: none. The user confirmed this one local commit on 2026-10-02.
