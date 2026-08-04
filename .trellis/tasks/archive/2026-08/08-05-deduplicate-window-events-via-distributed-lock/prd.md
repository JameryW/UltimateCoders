# Deduplicate window events via distributed lock

## Goal

The window_watcher (#557) emits `schedule.window.opened`/`closed` on transition — but with multi-instance gateways, ALL N instances run the watcher, so each transition fires N duplicate events. The distributed lock (#558) exists for cron-fire dedup; reuse it for window events.

## What I already know

* `window_watcher.rs` (uc-grpc-server) — polls `is_within_window`, publishes on transition. No lock.
* `NatsKvLockProvider` (#558, uc-grpc, scheduler_dispatch.rs) — `try_acquire(key, ttl)`, kv.create acquire-or-skip.
* `LockProvider` trait (uc-engine/scheduler/lock.rs) — `try_acquire(&self, key, &str, ttl: Duration) -> bool`.
* The watcher already has the nats_client (passed to `start_window_watcher`).

## The fix

In `window_watcher.rs`'s transition-publish path: before calling `publish_window_event`, acquire a lock with key `window:{event_type}:{tick_timestamp}` (per-transition). If acquire fails (another instance holds it), skip the publish. TTL ~60s (window transitions are coarse; the lock just deduplicates the one-time event).

## Implementation

1. `start_window_watcher` gains a `lock_provider: Arc<dyn LockProvider>` param (or build a `NatsKvLockProvider` inside if nats_client is available).
2. Before each `publish_window_event` call: `if !lock_provider.try_acquire(&lock_key, Duration::from_secs(60)).await { continue; }` (skip — another instance is publishing).
3. Lock key: `window:{opened|closed}:{Utc::now().timestamp()}` (per-transition, per-tick).

## Constraints

- Single-instance fallback: NoOpLockProvider (always acquire, no dedup) — current behavior.
- Feature-gated: messaging (the NatsKvLockProvider is messaging-gated).
- Don't rewrite the watcher — just add the lock check before publish.

## Acceptance Criteria

* [ ] window_watcher acquires lock before publishing transition
* [ ] Skip publish on lock-failure (dedup)
* [ ] NoOp fallback (single-instance, no regression)
* [ ] Tests: lock-acquired publishes, lock-failed skips, NoOp always publishes
* [ ] CI green

## Out of Scope

* The cron-fire lock (#558, already done)
* Sub-second precision (window transitions are coarse)

## Technical Notes

* `crates/uc-grpc-server/src/window_watcher.rs` — the watcher
* `crates/uc-grpc/src/scheduler_dispatch.rs` — NatsKvLockProvider (#558)
* `crates/uc-engine/src/scheduler/lock.rs` — LockProvider trait + NoOpLockProvider
