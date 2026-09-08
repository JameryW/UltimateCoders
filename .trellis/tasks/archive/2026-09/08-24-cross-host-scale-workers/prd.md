# Cross-Host ScaleWorkers (UC_SCALE_HOSTS fan-out)

## Goal

`ScaleWorkers` action="scale" shells out to `docker compose --scale worker=N`
against the LOCAL docker daemon only (AGENTS.md "future work"). Make the
gateway able to spread workers across multiple hosts **without** swarm mode,
a remote docker context, or a per-host gateway.

## Decision (ADR-lite)

**Context**: The gateway already composes a `docker compose up -d --no-deps
--scale worker=N worker` command. Docker's CLI accepts a per-invocation
daemon override via the `DOCKER_HOST` env var (`ssh://user@host` works out
of the box; no pre-registered contexts needed). Workers self-register to the
gateway over gRPC and pull code from an external git remote, so a worker on
any host that can REACH the gateway is already a first-class scheduling
citizen — scaling was the only single-host step in the chain.

**Decision**: Env-driven multi-host fan-out. `UC_SCALE_HOSTS` holds a
comma/semicolon list of per-host docker connection specs; the special value
`local` means "the daemon the gateway itself reaches" (no override — today's
behavior). The gateway splits `target_count` across hosts (even split,
first hosts take the remainder) and runs one compose command per host with
`DOCKER_HOST=<spec>` set for non-local entries. Unset/empty → `["local"]`:
byte-for-byte today's behavior, zero risk to existing deploys. No proto
change (client surfaces stay identical); partial failures aggregate into
`success=false` + per-host detail in `message`/`error`.

**Consequences**: One env var turns on multi-host; no new services, no swarm
primitives, no scheduler changes. Limits: per-host counts are absolute
(compose `--scale`), so concurrent ScaleWorkers calls converge to the last
write per host (same as today, per host); remote workers need
`UC_GATEWAY_ADDR` pointing at a gateway address reachable from their host
and `UC_REPO_URL` for code sync (deployment config, documented).

## Requirements

* `UC_SCALE_HOSTS="local,ssh://user@h2"` + scale(N) → N split across hosts,
  each host running its own compose command; failures don't abort other hosts.
* Unset/empty env → identical single-host behavior.
* Pure helpers (`parse_scale_hosts`, `split_target_across_hosts`) unit-tested;
  existing error paths unchanged.

## Out of Scope

* Swarm mode / remote docker contexts / per-host gateways.
* Proto changes (request-level host overrides) — revisit if per-call routing
  is ever needed.
* Autoscaling / drift reconciliation between calls.

## Completion Log (2026-08-24)

* `parse_scale_hosts` / `split_target_across_hosts` / `run_compose_scale`
  helpers in `uc-grpc/src/worker_service.rs`; scale action fans out per
  host with best-effort aggregation (`success` = all hosts OK,
  `actual_count` = sum of successful shares, failures joined in `error`,
  per-host detail in `message`). Unset env → single-host behavior
  byte-for-byte.
* +4 unit tests for the pure helpers (defaults, split/remainder, edges);
  existing error-path tests unchanged and green. uc-grpc 138 → 142 tests.
* Docs: AGENTS.md + CLAUDE.md cross-host section rewritten,
  `docker/.env.example` entry, worker-service-spec §6b + env table row.
* Verified: workspace clippy --all-targets zero warnings, fmt clean,
  full workspace test suite green, Python suite 947 passed.

### Addendum — dry-run plan mode (same day)

* `UC_SCALE_DRY_RUN` truthy (`1/true/yes/on`) → scale action returns the
  per-host plan (`DRY-RUN: … host=share …`) with `actual_count = target`
  WITHOUT invoking docker; compose-file pre-check skipped so the plan is
  inspectable on gateways without a local compose file. Unset → real
  execution, unchanged.
* +1 helper unit test (truthy/falsy variants) + Case 3 in the env-race
  test (dry-run succeeds despite missing compose file, no FAILED marker).
* Also: `dashboard_service` tests-module `use super::*` gated behind the
  `messaging` feature (all its tests are messaging-gated; unconditional
  import warned under default features).
* Docs: AGENTS.md/CLAUDE.md cross-host sections, `.env.example`,
  worker-service-spec §6b.
