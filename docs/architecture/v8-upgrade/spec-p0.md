# Spec P0 — Decision Record (V8 Decision / StateOS slice)

Blocked by: D1–D5 (decided). D6 does not block P0.

## Goal

Give every dispatch attempt a durable, log-safe **Decision Record** that joins model-routing choice, context-compile identity, and terminal verdict — the V8 "Decision Record & Audit" capability without inventing a second scheduler or storing prompt content.

## What to build

1. `python/ultimate_coders/decision_record.py` — `DecisionRecordOutbox` + `decision_key(task, subtask, attempt)` over `RuntimeState` namespace `decision_record`.
2. Worker hooks — record at attempt start (after route + optional context compile); merge terminal outcome when the attempt finishes.
3. Dashboard — `GET /dashboard/api/decisions?task_id=…[&subtask_id=…]` with the same auth / limit conventions as context manifests (key-order history is enough for P0; `order=recent` may reuse the same pattern if cheap).
4. Docs — link from `docs/architecture.md` and `docs/agent-os-architecture.md` to the V8 map; ticket 03.

## Acceptance criteria

- [x] One record per `task:subtask:attempt`; re-dispatch creates a sibling key, does not overwrite prior attempts.
- [x] Stored payload never contains prompt / tool stdout content.
- [x] Identity routes omit the `route` field (default event shape unchanged when no table).
- [x] Persistence errors are swallowed (debug log); subtask result unchanged.
- [x] Dashboard endpoint requires auth + `task_id`; returns `{decisions, count, total}`.
- [x] Unit tests cover key identity, start/terminal merge, no-content invariant, worker best-effort, and dashboard happy path.

## Out of scope (P0)

- Portable Capability Tokens
- Graph shadow default-on
- Learning / trajectory export
- Dashboard React panel (API only is enough; UI can follow)
- Unifying ProactiveAuditEntry rows into this store (governance keeps its own audit)
