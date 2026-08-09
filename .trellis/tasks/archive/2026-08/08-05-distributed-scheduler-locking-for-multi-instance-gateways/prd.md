# Distributed scheduler locking for multi-instance gateways

## Goal

When multiple gateway instances run (HA/scaling), each runs its own `SchedulerService`. A cron job fires on ALL instances simultaneously → duplicate task submissions (`NatsSubmitDispatcher` publishes N times → N duplicate tasks). Need distributed locking so only ONE instance fires each cron tick. Currently single-gateway (no coordination) — the memo's scheduler-spec even notes "single-gateway MVP".

## What I already know

* `SchedulerService` (`uc-engine/src/scheduler/service.rs`) — runs `tokio_cron_scheduler` per-instance. Each instance's cron callback fires independently.
* `NatsSubmitDispatcher` (#553) — publishes to `uc.task.submit` on cron-fire. Multiple instances = multiple publishes = duplicate tasks.
* JetStream infra exists (`UC_TASK_EVENTS` stream, `UC_SUBTASKS` stream #552). NATS KV (key-value store) is JetStream-based — available without new deps (async-nats has `jetstream().create_key_value()`).
* `distributed_conflict.py` `DistributedConflictDetector` — advisory same-process only (DashMap), NOT distributed. No cross-instance lock primitive exists.
* `WindowEventType`/`publish_window_event` (#557) — same multi-instance problem (all instances emit window events).

## The gap

No distributed lock primitive. A cron job firing on N gateways = N task submissions + N window events. Need: only one instance fires per cron tick.

## Open Questions

* **Lock primitive**: NATS KV (JetStream-based, `jetstream().create_key_value()`) with a TTL lease? OR a dedicated JetStream stream with a single-consumer queue (only one instance acks)? NATS KV with TTL is the standard "distributed lease" pattern.
* **Lock granularity**: per-cron-tick (lock = `scheduler:{job_id}:{tick_timestamp}`, short TTL) or per-job (longer lease)? Per-tick is correct — each fire is independently guarded.
* **Acquire-or-skip**: an instance that can't acquire the lock SKIPS (doesn't fire) — the lock holder fires. No queueing (the cron will tick again next period).
* **Lease TTL**: slightly longer than the cron-fire-to-publish duration (seconds). E.g. 30s TTL; the lock auto-releases if the holder crashes.
* **Layering**: the lock check goes in the cron callback (service.rs `register_cron_with_scheduler`) — but that's uc-engine (no NATS). Mirror #553/#557: the lock is acquired in a gateway-layer wrapper, OR inject a `LockProvider` trait into SchedulerService (default = always-acquires, single-instance).
* **Single-instance fallback**: when no NATS / no messaging feature, the lock is a no-op (always acquire) — current behavior, no regression.

## Requirements (evolving)

* A distributed lock primitive (NATS KV lease) so only one gateway instance fires each cron tick.
* Lock acquired before `dispatch_with_guard`; skip on lock-failure.
* TTL lease (auto-release on crash).
* Single-instance fallback (no-op lock = always acquire).
* Feature-gated (messaging).
* Tests: lock acquire/skip logic, TTL expiry, fallback no-op.

## Acceptance Criteria

* [ ] Distributed lock primitive (NATS KV lease)
* [ ] Cron callback acquires lock before firing; skips on failure
* [ ] TTL auto-release on crash
* [ ] Single-instance fallback (no-op)
* [ ] Tests
* [ ] CI green

## Out of Scope

* Leader election (full HA pattern) — per-tick lease is simpler + sufficient
* Window-event dedup (#557) — the lock also dedups window events if the watcher checks the lock (separate, or accept duplicate events as non-fatal)

## Technical Notes

* `service.rs:587` — cron callback (register_cron_with_scheduler) — the lock point
* `dispatcher.rs:292` — publish_window_event (also multi-instance)
* `nats_worker.py` — async-nats usage (KV API: `nc.jetstream().create_key_value(KeyValueConfig)`)
* [[scheduler-activation-feature-2026-08-03]] — single-gateway note
* [[grpc-json-to-key-mismatch-pattern]] — NATS conventions
