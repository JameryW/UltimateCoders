# T14: same-contract one-shot agent redispatch

## Goal

When `UC_STALL_REDISPATCH_ENFORCE` is on and a progress-stop `stall_signal` fires, redispatch the **same** contract digest once with an alternate registered coding agent. After a second stall (or no alternate), escalate to non-retryable terminal / human. Flag off preserves today’s stop-without-swap. Pure decision helpers live in `stall_redispatch.py` and are unit-testable without NATS.

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/14-same-contract-agent-redispatch.md`
- Spec: `docs/architecture/agent-trust-eval/spec-p2.md` (Seam G enforce half)
- Decision: D7 — MVP same-contract one-shot; true replan deferred (T18)
- Prerequisite: T13 observe surface already shipped

## What I already know

* T13 module + worker seam exist: `stall_redispatch.py`, worker attaches `loop_detected` + `stall_signal` on stop; flag default off; no swap yet.
* Worker stall path forces `result.retryable=False`; orchestrator `handle_subtask_result` does not auto-reopen Failed; gateway `retry_subtask` is the explicit Failed→Pending CAS.
* Agents: `UC_CODING_AGENT` + plugin `registry` / `available_agents()` / `Subtask.agent_config["agent"]`.
* Digest: `contract.contract_digest(user_request)` + `ContractRegistry`; StallSignal already has optional `contract_digest` (worker does not pass it yet).
* Research: [`research/grounding.md`](research/grounding.md)

## Assumptions (temporary)

* One stall episode = one subtask attempt chain until escalate or success; quota is exactly one agent swap.
* Alternate selection: first discoverable registered agent ≠ current canonical name (stable, deterministic order from registry list).
* Wire owner defaults to **orchestrator / dispatch path** per D7 (“编排层拥有”), with pure helpers in `stall_redispatch.py`.

## Decisions locked

* **Wire location = Approach A** (D7): orchestrator/dispatch Failed→Pending + `agent_config.agent` stamp. Pure helpers in `stall_redispatch.py`. Not worker-inline.

## Requirements (evolving)

* Reuse `UC_STALL_REDISPATCH_ENFORCE` (default off).
* Extend `stall_redispatch.py` with pure helpers: episode state, alternate selection, `decide_stall_redispatch` → `noop` | `swap_once` | `escalate`.
* Flag on + first stall + alternate available + same digest → one redispatch with alternate agent; do not bump contract version / rewrite goal.
* Second stall (or no alternate) → escalate (terminal non-retryable).
* Flag off → no swap (T13 behaviour).
* `progress.py` gains no replan/swap API.
* Prefer attaching `contract_digest` on stall_signal when `user_request` present.
* Tests without NATS for helpers; thin wire coverage for flag-off / swap-once / escalate.

## Acceptance Criteria (evolving)

* [ ] Flag default off → stall still stops; no agent swap
* [ ] Flag on → exactly one alternate-agent redispatch under unchanged digest
* [ ] Second stall → escalate; no further automatic swaps
* [ ] Does not bump contract version or rewrite goal text
* [ ] `progress.py` still has no replan / agent-swap primitive
* [ ] Pure helpers unit-tested without NATS
* [ ] Spec (`.trellis/spec/backend/stall-redispatch-spec.md`) updated for T14 enforce half

## Definition of Done (team quality bar)

* Tests added/updated (unit; wire smoke as needed)
* Lint / typecheck / CI green
* Backend stall-redispatch spec updated for enforce/swap contracts
* Rollout: flag remains default off

## Out of Scope (explicit)

* True replan / new decomposition (T18)
* NetworkMode / Landlock (T15+)
* Multi-swap cascades across all agents
* New env flag name
* Conflating envelope `CONTRACT_VERSION` with goal `contract_digest`

## Technical Approach (recommended)

**Approach A: pure helpers + orchestrator/dispatch wire** (Recommended)

* How: decision + alternate selection in `stall_redispatch.py`; on `swap_once`, dispatch owner reopens Failed→Pending (or equivalent), stamps `agent_config["agent"]`, preserves `user_request`; episode `swaps_used=1`; second stall escalates.
* Pros: matches D7 ownership; helpers testable offline; clear separation from progress detector.
* Cons: needs a careful thin wire so attempt identity / outbox stay coherent.

**Approach B: worker-inline swap**

* How: on first stall inside worker retry/stop path, if enforce, switch sandbox agent and continue once before terminal.
* Pros: fewer cross-process seams.
* Cons: weaker “orchestrator owns” story; easier to confuse with inner MAX_RETRIES; harder to observe as redispatch.

## Research References

* [`research/grounding.md`](research/grounding.md) — key paths, API shape, what not to invent

## Technical Notes

* Key files: `stall_redispatch.py`, `worker.py` (~1229+), `nats_worker.py` (contract gate + result events), `orchestrator.py` (`handle_subtask_result`), `registry.py`, `contract.py`, gateway `retry_subtask`
* Existing flag constant: `STALL_REDISPATCH_ENFORCE_ENV`
