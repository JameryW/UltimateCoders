# Integrate the MetaInfer inference domain

Status: done
Category: enhancement

## What to build

Deliver the adapter, shared benchmark/oracle, InfraAgent/router and execution-adaptation graph as one integrated vertical slice. Source specification: ../spec.md and the user's attached integration plan.

## Acceptance criteria

- Typed tool operations submit schema-valid tasks to the actual upstream API, retain evidence/artifact references, and fail clearly on unsupported types or transport/process errors.
- Timeout and cancellation attempt to terminate the created remote job and never report success.
- Oracle rejects failed compilation/correctness, missing/nonfinite metrics, incomparable workloads, memory violations and performance regressions.
- Optimization iterations preserve improvements and restore rejected/errored candidates in an owned clean worktree; original dirty workspaces are refused.
- Opt-in routing carries explicit inference task configuration through submission, capabilities and sandbox execution; generic tasks and explicit agent overrides retain their behavior.
- A validated graph carries model/runtime/dispatch/kernel/hardware/evidence relationships and benchmark history into artifacts and accepted project memory.
- Focused integration tests, full Python suite and lint pass; deployment configuration and usage are documented.

## Blocked by

None.

## Delivery

Implemented on `codex/metainfer-infra`. Full Python regression: 1335 passed, 1 skipped, 8 external-infrastructure tests deselected; 50 domain tests pass. Standards/Spec reviews, lint, repository guards and Compose rendering pass. POSIX containment was also checked in WSL. GPU performance and production image execution require the external deployment environment.
