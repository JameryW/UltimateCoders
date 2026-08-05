# Wire Python night_window_active exclusive mode from window events

## Goal

The scheduler-spec (line 327) documents: "When the night window is active, the Orchestrator enters exclusive mode: scheduled tasks bypass the queue and execute immediately, while real-time tasks are deferred to `_pending_tasks` until the window closes." The window events (`schedule.window.opened/closed`, #557) are now emitted, but the Python Orchestrator has NO consumer — no `night_window_active` flag, no exclusive-mode logic, no subscription to the events. Wire it.

## What I already know

* `orchestrator.py` — no `night_window_active` field, no exclusive-mode logic (grep confirmed).
* `orchestrator.py:699` — `flush_pending_tasks()` exists (drains pending → re-executes).
* `nats_worker.py:2720` — calls `orch.flush_pending_tasks()` (the flush path exists).
* Window events (#557) — `schedule.window.opened`/`closed` published on transition (deduped via #559).
* `orchestrator.py:55` — `night_window: Any = None` field exists (dead-ish — the spec's exclusive mode isn't wired).

## The gap

1. Orchestrator has no `night_window_active` flag.
2. No NATS subscription for `schedule.window.opened/closed`.
3. No exclusive-mode logic: when active, real-time tasks → `_pending_tasks`; when inactive, `flush_pending_tasks()` drains.

## Implementation

1. **Orchestrator gains `night_window_active: bool`** (default False). Settable via a method (e.g. `set_night_window_active(bool)`).
2. **`nats_worker.py`** subscribes to `schedule.window.opened`/`closed` (new subscriptions in the default-mode block, like the existing `uc.task.submit` subscription). On `opened`: `orch.set_night_window_active(True)`. On `closed`: `orch.set_night_window_active(False)` + `orch.flush_pending_tasks()`.
3. **Exclusive-mode logic in `submit_task`/dispatch**: when `night_window_active`, real-time (non-scheduled) tasks → `_pending_tasks` instead of immediate dispatch. When inactive, immediate dispatch (current behavior).
   - But: scheduler-fired tasks (from `NatsSubmitDispatcher`) come through `uc.task.submit` → `_handle_submit` → `submit_task`. How to distinguish "scheduled" (bypass) vs "real-time" (defer)? The NatsSubmitDispatcher payload could carry a flag (e.g. `scheduled: true`), OR the distinction is by source. Recommend: the payload gains a `scheduled` field (from `NatsSubmitDispatcher` → True; from `TaskService::submit_task` gRPC → False/absent). When `night_window_active` + `not scheduled` → defer to pending.
4. **`_pending_tasks`**: a list on the orchestrator (or existing structure). `flush_pending_tasks` drains it.

## Open Questions

* **Distinguishing scheduled vs real-time**: payload `scheduled` flag (recommended) OR a separate NATS subject for scheduled submits? Recommend: flag in the payload (NatsSubmitDispatcher sets it).
* **Exclusive-mode scope**: does "real-time tasks defer" mean ALL non-scheduled submits, or only certain types? Spec says "real-time tasks" — interpret as all non-scheduled (user-initiated via gRPC). Recommend: all non-scheduled.
* **Flush trigger**: on `closed` event, flush ALL pending. But if a task was deferred and the window reopens, should it stay pending until the next close? Recommend: flush on close (run the backlog), defer again on reopen.

## Requirements (evolving)

* Orchestrator `night_window_active` flag + setter.
* `nats_worker` subscribes to `schedule.window.opened/closed`, toggles the flag, flushes on close.
* `submit_task`: when `night_window_active` + not scheduled → defer to `_pending_tasks`.
* `flush_pending_tasks`: drains `_pending_tasks` (run the backlog).
* Payload `scheduled` flag from `NatsSubmitDispatcher`.

## Acceptance Criteria

* [ ] Orchestrator has `night_window_active` flag + setter
* [ ] `nats_worker` subscribes to window events, toggles + flushes
* [ ] `submit_task` defers non-scheduled tasks when active
* [ ] `flush_pending_tasks` drains the backlog
* [ ] Tests: flag toggle, defer/flush, payload scheduled flag
* [ ] CI green

## Out of Scope

* The Rust-side window watcher (#557/#559 — done)
* Dashboard UI for exclusive mode
* Per-task night-window override (existing)

## Technical Notes

* `orchestrator.py:55,699` — night_window field + flush_pending_tasks
* `nats_worker.py:469-476` — default-mode NATS subscriptions (the subscription block)
* `nats_worker.py:2720` — existing flush call
* `.trellis/spec/backend/scheduler-spec.md:327` — the exclusive-mode contract
* #557 (window events) + #559 (dedup) — the event source
* #553 (NatsSubmitDispatcher) — where the `scheduled` flag would go
