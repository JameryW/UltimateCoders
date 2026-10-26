# T13: stall signal surface

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/13-stall-signal-surface.md`
- Spec: `docs/architecture/agent-trust-eval/spec-p2.md` (Seam G observe half)
- Decision D7: MVP same-contract one-shot redispatch is **ticket 14**; this ticket is observe-only

## Goal

1. New small module (prefer `python/ultimate_coders/agent/stall_redispatch.py`):
   - `STALL_REDISPATCH_ENFORCE_ENV = "UC_STALL_REDISPATCH_ENFORCE"`
   - `stall_redispatch_enforce_enabled()` — truthy parser; **default off**
   - Frozen `StallSignal` (or equivalent) with stable fields: at least `loop_detected`, `stalled_streak`, `reason`, `error_signature` (and optional `subtask_id` / `contract_digest` when caller has them)
   - `build_stall_signal(verdict, **context) -> StallSignal | None` — returns a signal when `verdict.action == "stop"`, else `None`
   - `StallSignal.to_dict()` for event payloads
2. Worker seam (`worker.py` near existing `loop_detected` / `progress` failure_data):
   - Always attach structured `stall_signal` (or keep `loop_detected` + enrich) when progress verdict is stop, so NATS/orchestrator consumers do not scrape logs
   - Flag does **not** swap agents in T13; flag off preserves today’s stop-without-redispatch
   - Do **not** add replan / agent-swap APIs to `progress.py`
3. Trellis backend spec documenting the contract + flag default off

## Out of scope

Same-contract agent redispatch (T14); true replan (T18); NetworkMode (T15+); changing stall_limit math.

## Tests

`tests/python/test_stall_signal_surface.py`: flag default off; build_stall_signal on stop vs retry; to_dict shape; worker/helper seam does not invent replan in progress.py.
