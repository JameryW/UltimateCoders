# Execution plan

1. Build Oracle and typed benchmark contracts with a failing regression acceptance test.
2. Implement command benchmarking, execution graph and transaction-backed optimization workflow; test acceptance/rollback/error/cancel.
3. Implement schema-driven MetaInfer HTTP submission, polling, cancellation and artifact capture; test actual pinned contracts.
4. Add InfraAgent/domain router and subprocess adapter; integrate planner/Worker/Docker configuration and accepted shared memory.
5. Document usage and evidence protocol, run code review and Trellis check, update spec and commit locally.

Validation: the four `test_inference_*` modules, the complete Python suite excluding integration, `ruff check python/ tests/`, repository spec/task/line-ending checks, and Compose rendering.

## Completed implementation and validation

- Implemented schema-driven external MetaInfer tools, task routing/capabilities, UC subprocess adapter, immutable benchmarks/Oracle, iterative rollback, adaptation graph, persistent artifacts and accepted project Memory. Added explicit Dashboard/NATS configuration forwarding and Docker environment/storage support.
- Two independent code-review axes found cancellation, descendant cleanup, rollback and output propagation defects. All findings were repaired and re-reviewed without remaining blockers.
- Full Python regression: **1335 passed, 1 skipped, 8 deselected** (external infrastructure tests), 86.11 seconds. The only warning is the existing Starlette/httpx deprecation.
- Domain suite: **50 passed**, including real sandbox subprocess execution, both HTTP cancellation seams, file/directory rollback, fixed workload acceptance and forced nested-session termination.
- Python lint, spec/task reference audits, Codex issue-flow/workflow-input checks, line-ending guard (1934 files/1925 text, 9 binary/1 gitlink) and Compose config/build-plan rendering passed.
- WSL Ubuntu 24.04/Python 3.12 smoke checks passed for actual POSIX nested-session termination and refusal to execute before registration. These use standard-library tests, not the full Linux suite.
- Docker daemon is stopped, so production image execution was not tested. Real GPU measurements require an external MetaInfer service, model weights and a shared assigned workspace.

Local branch: `codex/metainfer-infra`. One feature commit contains this coherent delivery; no remote push is part of the task.
