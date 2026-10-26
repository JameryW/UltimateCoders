# 13: stall signal surface

**What to build:** Make progress-stop / `loop_detected` a first-class, observable dispatch signal (events/metrics/structured result fields) so orchestrator code can react without scraping logs. Feature flag `UC_STALL_REDISPATCH_ENFORCE` defaults off and does **not** yet swap agents (that is ticket 14). Do not add a replan API inside the progress detector.

**Blocked by:** None.

**Status:** ready-for-agent

- [ ] Progress-stop / `loop_detected` is visible to orchestrator / NATS consumers as structured data
- [ ] Flag `UC_STALL_REDISPATCH_ENFORCE` exists and defaults off
- [ ] Flag off preserves today’s stop-without-redispatch behaviour
- [ ] Progress detector module gains no replan / agent-swap primitive
- [ ] Tests cover signal emission without requiring a live cluster
- [ ] Feature flag defaults off
