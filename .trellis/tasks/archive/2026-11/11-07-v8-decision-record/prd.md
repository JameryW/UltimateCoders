# PRD — V8 Decision Record (P0)

## Background

User requested upgrading UltimateCoders against the V8 Distributed Agent · Decision · Inference · Training architecture. Wayfinder `docs/architecture/v8-upgrade/` settled D1–D6: UC hardens Agent+Decision+Evidence; Inference stays external; Learning deferred. P0 is the unified Decision Record.

Source tickets: `docs/architecture/v8-upgrade/tickets/01-*.md`, `02-*.md`, `03-*.md`.

## Requirements

1. Persist one Decision Record per dispatch attempt (`task:subtask:attempt`) in `RuntimeState` namespace `decision_record`.
2. Start facts: worker_id, optional route, context digest + key (no prompt content).
3. Terminal facts merged into the same key on attempt completion.
4. Best-effort persistence (never fail the subtask).
5. Dashboard `GET /dashboard/api/decisions` with auth.
6. Docs: ADR + architecture / agent-os links + V8 map (already drafted; keep in sync when code lands).

## Out of scope

- Capability tokens, graph shadow default, Learning OS, React panel, ProactiveAudit unification.

## Test seams

- `tests/python/test_decision_record.py`
- `tests/python/test_dashboard_decisions.py`
- Mirror patterns from `test_context_outbox.py` / `test_dashboard_context_manifests.py`

## Done when

All P0 acceptance criteria in `spec-p0.md` checked; focused pytest green.
