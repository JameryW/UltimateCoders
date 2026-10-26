# 13: stall signal surface

**What to build:** Make progress-stop / `loop_detected` a first-class, observable dispatch signal (events/metrics/structured result fields) so orchestrator code can react without scraping logs. Feature flag `UC_STALL_REDISPATCH_ENFORCE` defaults off and does **not** yet swap agents (that is ticket 14). Do not add a replan API inside the progress detector.

**Blocked by:** None.

**Status:** implemented (branch `cursor/t13-stall-signal-surface-fb91`; `STALL_REDISPATCH_ENFORCE_ENV` / `stall_redispatch_enforce_enabled` default off; frozen `StallSignal` + `build_stall_signal` on progress stop; worker attaches structured `stall_signal` beside `loop_detected`; no replan/swap in `progress.py`; agent swap deferred to ticket 14)

- [x] Progress-stop / `loop_detected` is visible to orchestrator / NATS consumers as structured data
- [x] Flag `UC_STALL_REDISPATCH_ENFORCE` exists and defaults off
- [x] Flag off preserves today’s stop-without-redispatch behaviour
- [x] Progress detector module gains no replan / agent-swap primitive
- [x] Tests cover signal emission without requiring a live cluster
- [x] Feature flag defaults off
