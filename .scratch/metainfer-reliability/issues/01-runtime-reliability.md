# Repair inference execution and experiment delivery

Status: completed
Category: bug, enhancement
Blocked by: none

Implementation and verification are complete. The user confirmed the one local
repair commit on 2026-10-02; no remote publication is pending.
See the repository verification report and Trellis review artifact for evidence.

## What to build

Complete the accepted MetaInfer architecture review as one integrated runtime repair.

## Acceptance criteria

- Mutating inference tasks and workflow steps require a real, exclusively leased worktree,
  independent of file constraints; allocation failure cannot fall back to shared code.
- Accepted changes are committed by UC and survive merge/release; unsuccessful delivery retains
  recoverable code, and evidence distinguishes acceptance from committed/merged delivery.
- Persist operation intent and remote IDs against UC execution identity. Resume known jobs without
  another POST. Submission ambiguity and unconfirmed termination prevent retry, rollback and release.
- Persist terminal results before ACK and retry publication independently of expensive execution.
- Advertise live operation-specific plugin capabilities; local benchmarking needs no MetaInfer URL.
  Refresh unhealthy services and enforce shared-backend concurrency/time budgets.
- Persist stable experiment manifests, atomic iteration checkpoints and integrity-checked artifacts;
  expose authorized experiment/artifact queries through the Dashboard API.
- Benchmark repeated samples, warmup, dispersion and environment/workload identity; Oracle rejects
  unstable or insufficient improvements and records the evidence used for its verdict.
- Emit experiment phases and cleanup state through existing task events. Exercise job failure,
  cancellation, restart/adoption, lost result publication, code delivery and artifact access.
- Update deployment configuration, architecture/inference docs and executable specs. Run focused
  regression tests, relevant complete suites and quality guards; commit reviewed changes.

## Validation boundary

Tests may use isolated Git repositories, HTTP fixtures and a local PostgreSQL instance. A real
GPU speedup requires actual MetaInfer/model/hardware configuration and must not be inferred from
synthetic contract fixtures. Preserve this distinction in documentation and the final report.
