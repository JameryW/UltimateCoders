# Migrate uc.subtask.execute to JetStream durable consumer

## Goal

Switch subtask dispatch from core NATS (queue group "workers", no redelivery — a worker crash mid-execution loses the subtask forever) to a JetStream durable consumer. Enables cross-host crash recovery: if a worker dies mid-subtask, NATS redelivers to another worker. The JetStream infrastructure already exists (UC_TASK_EVENTS stream + dashboard-replay consumer) — this adds a UC_SUBTASKS stream + durable pull consumer for `uc.subtask.execute`.

## What I already know

* **Core NATS path** (`nats_worker.py:417-421`): `subscribe(NATS_SUBJECT_SUBTASK_EXECUTE, queue="workers", cb=self._handle_subtask_execute)`. Queue group = exactly-once AMONG live workers, but NO redelivery if the receiving worker crashes mid-execution. The comment at line 1639-1644 confirms: core NATS = no `nak`/redelivery, `subtask_dispatch_rejected` event is the signal.
* **JetStream already used** for `uc.task.event` (line 621 `_ensure_jetstream_stream` → UC_TASK_EVENTS stream; line 655 `_ensure_jetstream_consumer` → dashboard-replay consumer, F65 pull/push fix). Infrastructure exists; this reuses the pattern.
* **Capability NACK** (line 1625-1645): a worker missing capabilities publishes `subtask_dispatch_rejected` and returns. JetStream redelivery would re-deliver to the SAME missing-cap worker unless the consumer filter/queue excludes it — need to handle (the rejection event lets the orchestrator keep it Pending, so redelivery is acceptable but could hot-loop).
* **Worker crash model**: worker holds a subtask in `_bg_tasks` (line 1676). On `stop()`, the bg tasks are cancelled (line 603-606). With core NATS, the message is gone. With JetStream, the consumer's last-acked position means the message redelivers after the consumer reconnects (different worker instance).

## The gap

Core NATS subtask delivery = lost subtasks on worker crash. CLAUDE.md notes this as a known limitation ("core NATS, no redelivery"). The authoritative cross-worker conflict point is git merge-time (MergeArbiter), but a mid-execution crash still loses the subtask's work-in-progress + the subtask stays in_progress on the gateway (stale).

## Open Questions

* **max_deliver + poison subtask**: cap redelivery count (avoid hot-loop on a subtask that always crashes its worker or always NACKs)? What happens at the cap — Failed terminal, or dead-letter?
* **Fallback**: if JetStream unavailable (non-fatal, mirrors existing pattern), fall back to core NATS queue group (current behavior)?
* **Backward compat**: old workers still on core NATS queue group — coexistence? (Stream subject served to both, but ACK semantics differ.)

## Decision (ADR-lite)

**Context**: uc.subtask.execute is core NATS (no redelivery — crash loses subtask). JetStream infra exists for uc.task.event.
**Decision**: **Shared durable pull consumer, ack-after-execution.** Work-queue retention; NATS delivers each subtask to one worker; worker acks AFTER `_execute_and_report` completes (success OR failure). Crash mid-subtask = no ack = redelivery to another worker.
**Poison handling**: max_deliver=5 → worker publishes `subtask_failed` ("max redelivery exceeded") + term-acks (acks so NATS stops redelivering). Subtask → terminal Failed (no hot-loop).
**Fallback**: JetStream unavailable → fall back to core NATS queue group (current behavior). New workers prefer JetStream; old core-NATS workers coexist (mixed mode; only JetStream redelivers).
**Consequences**: ACK timing load-bearing (ack-on-receipt defeats recovery). Capability NACK still publishes `subtask_dispatch_rejected`; shared durable picks another worker. Mixed-mode (old+new workers) means a subtask may hit a core-NATS worker (no redelivery) or a JetStream worker (redelivers) — acceptable during rollout.

## Requirements (evolving)

* New `UC_SUBTASKS` JetStream stream (subject `uc.subtask.execute`, work-queue retention).
* Worker subscribes via JetStream durable pull consumer (shared durable, ack-after-execution).
* Crash mid-subtask → redelivery to another worker (the cross-host recovery goal).
* max_deliver cap (poison-subtask guard) + escalation (subtask → Failed/terminal after N attempts).
* Fallback to core NATS queue group if JetStream unavailable.
* Capability NACK: still publishes `subtask_dispatch_rejected`; consumer doesn't re-deliver to the same worker (queue-group semantics pick another).
* Tests: redelivery-on-crash, max_deliver cap, fallback path, ack-timing.

## Acceptance Criteria

* [ ] UC_SUBTASKS stream created at worker start
* [ ] Subtask delivered via JetStream durable consumer
* [ ] Worker crash mid-subtask → redelivery to another worker
* [ ] max_deliver cap reached → subtask terminal (Failed/Pending, not hot-loop)
* [ ] JetStream unavailable → falls back to core NATS queue group (no regression)
* [ ] Capability NACK doesn't hot-loop (consumer picks another worker)
* [ ] ACK happens AFTER `_execute_and_report` (not on receipt)
* [ ] Tests pass (redelivery simulation, fallback, ack-timing, max_deliver)

## Definition of Done

* Tests added (redelivery, fallback, cap, ack-timing)
* Lint/CI green
* CLAUDE.md updated (the "core NATS, no redelivery" note changes to "JetStream durable, redelivery on crash")
* nats-bridge-spec updated

## Out of Scope

* Multi-instance worker coordination (the consumer handles that)
* Changing `uc.task.event` (already JetStream)
* MergeArbiter changes (git merge-time is separate)

## Technical Notes

* `nats_worker.py:417-421` — current core NATS subscribe
* `nats_worker.py:621` — `_ensure_jetstream_stream` (pattern to mirror)
* `nats_worker.py:655` — `_ensure_jetstream_consumer` (pattern, F65 pull/push fix)
* `nats_worker.py:1676` — `_spawn_bg(self._execute_and_report(subtask))` (bg task; crash cancellation)
* `nats_worker.py:1639-1644` — core NATS no-redelivery comment
* `.trellis/spec/backend/nats-bridge-spec.md` — NATS conventions
* CLAUDE.md "core NATS, no redelivery" note
