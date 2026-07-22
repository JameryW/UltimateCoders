# fix-jetstream-replay-stalls-on-malformed-event

## Goal

JetStream event replay (`_replay_missed_events` in `nats_worker.py`) permanently stalls when a single malformed event fails to handle: the `except Exception` at line 746 swallows the failure without acking the message or advancing `_js_last_seq`, so the same event is re-fetched and re-failed on every restart. Replay is a dashboard-display-only path, so the right behavior is to skip a permanently-broken event, not block forever.

## What I already know

* `nats_worker.py:738-754` — replay loop: `for msg in msgs:` → try `_handle_task_event` + `msg.ack()` + advance `_js_last_seq` → `except Exception: logger.debug(...)`.
* On failure: `msg.ack()` NOT called, `_js_last_seq` NOT advanced. Loop continues to next msg but seq stays at last-successful. After restart, `_save_js_seq(self._js_last_seq)` persisted the stale seq → re-fetch from stale point → same malformed msg re-fails → infinite stall.
* `msg.metadata.sequence.stream` (line 745) is the stream seq, already used (ponytail F65).
* Replay is display-only (dashboard event log). Not a write path. Skipping a broken event loses one dashboard entry — acceptable vs permanent blockage.
* JetStream pull consumer durable=`dashboard-replay`, subject `uc.task.event`, batch capped at 500 (ponytail bounded).
* Existing pattern: F65 comment already documents the team's prior fix of an adjacent seq-advance bug here.

## Root cause

`except` block neither acks nor advances seq. A malformed event (old schema, corrupt JSON, `_handle_task_event` bug) becomes an immovable replay blocker.

## Requirements

* On per-event handling failure: ack the message (skip it) so JetStream does not redeliver it.
* Advance `_js_last_seq` to the failed message's stream seq so replay progresses past it.
* Log at `warning` (not `debug`) with event context so operators see skipped events.
* Successful events keep current behavior (handle + ack + advance + debug-success).

## Acceptance Criteria

* [ ] Malformed event in replay batch: acked + seq advanced + warning logged, replay completes.
* [ ] Successful events unaffected.
* [ ] After restart following a previously-failing event, replay does NOT re-fetch the skipped event.
* [ ] Unit test: inject a msg whose `_handle_task_event` raises → assert ack called, seq advanced, no re-fetch loop.

## Definition of Done

* Tests added/updated.
* Lint / typecheck / CI green.
* Behavior change noted in commit message.

## Technical Approach

Restructure the per-message try/except so the ack + seq-advance are NOT skipped on handling failure. Two minimal variants:

**Approach A (Recommended): ack-and-advance in both branches.** Pull `msg.ack()` + `_js_last_seq = msg.metadata.sequence.stream` out of the try so they always run; keep `_handle_task_event` in try; on except, log warning with seq + event-type hint. ~6 lines changed.

**Approach B: nack-and-continue.** `await msg.nak()` on failure so JetStream redelivers later. Rejected — redelivery of a permanently-broken event loops forever; defeats the fix.

Approach A matches the "display-only, skip-broken" intent.

## Decision (ADR-lite)

**Context**: replay stall on malformed event, display-only path.
**Decision**: Approach A — ack-and-skip on handling failure, advance seq regardless, log warning.
**Consequences**: one broken event silently absent from dashboard (acceptable). Operators see warning. No permanent replay block.

## Out of Scope

* Fixing `_handle_task_event` itself against malformed payloads (separate concern; F349 already added replay-skip for synthetic TaskCreated-on-parse-fail at the handler level — this task is the replay-loop safety net, complementary).
* JetStream consumer config changes.
* Non-replay event paths.

## Technical Notes

* File: `python/ultimate_coders/nats_worker.py` lines ~738-754 (`_replay_missed_events`).
* Test location: `tests/python/` — look for existing nats_worker replay test pattern.
* Related: PR #349 (35328df4) — handler-level malformed-event skip; this is loop-level.
