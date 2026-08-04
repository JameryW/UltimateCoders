# Emit night-window open/close events on transition

## Goal

The night-window events (`schedule.window.opened`/`schedule.window.closed`) are defined (`publish_window_event` at dispatcher.rs:292, feature-gated messaging) but NEVER emitted. The spec (scheduler-spec.md:135) says they're published on window open/close, but `SchedulerService` has no transition detection. A Python orchestrator's `night_window_active` flag depends on these (dispatcher.rs:286 comment). Wire a periodic check that publishes on transition.

## What I already know

* `publish_window_event(nats_client, event_type, window_info)` (dispatcher.rs:292) — complete, messaging-gated, publishes to `schedule.window.opened`/`closed` with `{event, window_info, timestamp}`. Never called.
* `WindowEventType::Opened`/`Closed` enum (dispatcher.rs:332).
* `NightWindow::is_within_window(now)` (night_window.rs) — the check.
* `SchedulerService::start()` (service.rs:457) — starts the JobScheduler; a background task could be spawned here.
* `SchedulerService` has `night_window: Arc<RwLock<Option<NightWindow>>>` (the configured window).
* The service has no `nats_client` (it's in uc-engine, lower layer — same layering issue as NatsSubmitDispatcher, #553). But `publish_window_event` takes a `&async_nats::Client`.

## The gap

No periodic check of `is_within_window` + transition detection. Need:
1. A background task (spawned at `start()`) that checks `is_within_window(now)` periodically.
2. Tracks the last-known window state (inside/outside).
3. On transition (outside→inside = Opened, inside→outside = Closed), calls `publish_window_event`.
4. Only when a night_window is configured + messaging feature + nats_client available.

## Open Questions

* **Layering**: `SchedulerService` (uc-engine) has no `nats_client`. The window-check background task needs NATS to publish. Options:
  (a) Inject the nats_client into SchedulerService (uc-engine gains async-nats — layer violation, same as #553's dispatcher issue).
  (b) Spawn the window-check task in the GATEWAY (uc-grpc-server main), which has NATS, after start_scheduler. The task holds a clone of the SchedulerService + the nats_client, checks is_within_window, publishes on transition.
  **Recommend (b)** — mirrors #553's NatsSubmitDispatcher layering (NATS-using code in the gateway, not uc-engine).
* **Check interval**: how often to poll? Night windows are coarse (HH:MM), so a 60s poll is fine (no sub-minute precision needed).
* **No night_window configured**: skip the task entirely (no events needed).
* **State tracking**: the task holds `last_inside: bool` (or an atomic). On each poll, compute `now_inside = is_within_window(now)`; if `now_inside != last_inside`, publish the transition.

## Requirements (evolving)

* A background task (gateway, uc-grpc-server) that periodically checks the SchedulerService's night window + publishes open/close events on transition.
* Spawned at gateway start (after start_scheduler), only when night_window is configured + NATS connected + messaging feature.
* 60s poll interval.
* Tracks last_inside state; publishes on transition.
* No-op when no night_window (task not spawned).

## Acceptance Criteria

* [ ] Gateway spawns a window-check task when night_window configured + NATS available
* [ ] Transition outside→inside publishes `schedule.window.opened`
* [ ] Transition inside→outside publishes `schedule.window.closed`
* [ ] No-op when no night_window / no NATS
* [ ] Tests: transition detection (mock is_within_window), publish calls, no-op paths
* [ ] CI green

## Out of Scope

* Sub-minute precision (60s poll is fine)
* The Python orchestrator's `night_window_active` consumer (separate — the events just need to be published)

## Technical Notes

* `dispatcher.rs:292` — publish_window_event (the emitter)
* `dispatcher.rs:332` — WindowEventType enum
* `night_window.rs` — NightWindow::is_within_window
* `service.rs:457` — start() (spawn point consideration)
* `service.rs` — night_window field (Arc<RwLock<Option<NightWindow>>>)
* `uc-grpc-server/src/main.rs:144` — start_scheduler (gateway, has NATS)
* [[scheduler-activation-feature-2026-08-03]] — layering precedent (NatsSubmitDispatcher in gateway)
