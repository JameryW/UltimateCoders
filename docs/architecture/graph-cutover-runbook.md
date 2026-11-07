# Graph-primary cutover runbook (V8 P4 / D6 / D10)

Status: **ready-for-human**. Compose and image defaults stay **off**. This document
does not flip `UC_GRAPH_SHADOW`; an operator must authorize enablement explicitly.

Authority: [D6](v8-upgrade/issues/D6-graph-cutover-ownership.md),
[D10](v8-upgrade/issues/D10-graph-cutover-runbook.md),
[spec-p4](v8-upgrade/spec-p4.md). Durable-runtime lockstep restart rules live in
[durable-runtime-upgrade-notes.md](durable-runtime-upgrade-notes.md). Product
context: [architecture.md](../architecture.md) § Durable Runtime.

## What this cutover is (and is not)

| Layer | Role after shadow enable |
| --- | --- |
| TaskStore HashMap projection | Remains the **runtime authority** for task reads while shadow is on |
| PostgreSQL graph tables | Receive fire-and-forget shadow upserts; startup shadow-diff is warn-only |
| Graph-backed attempt / lease / fence / commit | Wired alongside the projection when shadow is on and storage is built |
| Graph-only TaskStore removal | **Out of scope** — still migration direction, not automatic |

**Never** infer graph-only authority from `UC_DATABASE_URL` alone. A database URL
initializes graph tables and backfill; it does **not** enable shadow mode.

Dry-run readiness (no mutation): `python scripts/check-graph-cutover-readiness.py`.

## Prerequisites

1. **PostgreSQL** reachable via non-empty `UC_DATABASE_URL` (same URL the task
   backend uses). Without it, graph store setup is skipped.
2. **Gateway built with the `storage` feature.** If `UC_GRAPH_SHADOW=on` but the
   binary lacks storage, the gateway logs a warn and skips shadow wiring.
3. **Gateway / worker contract lockstep.** Follow
   [durable-runtime-upgrade-notes.md](durable-runtime-upgrade-notes.md) §1:
   upgrade and restart gateway and workers in one lockstep window (no gray
   rollout for contract version). Prefer **gateway first**, then workers, per
   `AGENTS.md` contract-version notes.
4. Ops authorization recorded (who enabled, when, which environment). Defaults
   remain off until that authorization.

## Enable (explicit gate)

The gateway gate is exact token match — same decision function as
`resolve_graph_shadow_choice` in `crates/uc-grpc-server/src/main.rs`:

- **On:** `UC_GRAPH_SHADOW=on` (exact lowercase `on` only)
- **Off (default):** unset, empty, `1`, `true`, `On`, `OFF`, or any other value

Suggested sequence:

1. Run `python scripts/check-graph-cutover-readiness.py` and clear hard blockers.
2. Confirm `UC_DATABASE_URL` is set on the gateway process / Compose service env
   (do **not** edit tracked Compose defaults to turn shadow on).
3. Set **only** `UC_GRAPH_SHADOW=on` on the gateway (override / secrets / ops
   env — not a repo default).
4. Restart the gateway (and workers if images or contract also moved) per the
   durable-runtime upgrade notes.
5. Confirm startup log: `Graph shadow mode enabled (UC_GRAPH_SHADOW=on)`.

## Verify after enable

- Startup shadow-diff: diffs are **warn-only**; HashMap stays authority. A storm
  of diffs means investigate projection vs graph rows before going further.
- Lease / fence / commit-once paths exercise through normal dispatch; watch
  gateway health / Dashboard for stale-dispatch and worker registration health
  (see durable-runtime upgrade notes §3).
- Re-run the dry-run script: it should report shadow **on** and database URL
  **set**, and still must not mutate env or Compose.

## Rollback

1. Unset `UC_GRAPH_SHADOW` or set it to any value other than exact `on`.
2. Restart the gateway so shadow wiring is not re-applied.
3. TaskStore projection continues to serve; graph tables remain as data but are
   no longer written on persist.
4. If a full durable-runtime image rollback is required, follow
   [durable-runtime-upgrade-notes.md](durable-runtime-upgrade-notes.md) §4
   (old images + lockstep restart / import path).

## Defaults that must stay off

- Tracked Compose / image defaults for `UC_GRAPH_SHADOW` remain **off / unset**.
- This runbook and `scripts/check-graph-cutover-readiness.py` **never** set the
  env var and **never** rewrite Compose files.
- True replan **execute** / contract digest bump (trust-eval ticket 18 / T18)
  remains deferred; V8 P3 only landed proposal-only.

## Related

- V8 map: [v8-upgrade/map.md](v8-upgrade/map.md)
- Agent OS gap table: [agent-os-architecture.md](../agent-os-architecture.md) §5
