# 08: approx prune-only path and auto-disable

**What to build:** An approx scorer that emits discard/keep only, wired as a pre-filter before full Oracle (workflow / MetaInfer candidate fan-in — not inside Oracle evaluate). Prune runs only when enforce flag and agreement arming (ticket 07) are both on. Public uplift helpers must refuse approx scores. Bench or harness identity change forces re-agreement; failure auto-disarms approx.

**Blocked by:** 07 (rank-agreement gate + arming latch).

**Status:** implemented (branch `cursor/t8-approx-prune-autodisable-7c92`; prune via `approx_eval_ready_to_prune`; uplift refuses `source=approx` / `approx_score`; fingerprint retest auto-disarms)

- [x] Approx scorer returns prune decisions only (never OracleVerdict / public_uplift)
- [x] Wire prune-only upstream of Oracle; survivors still take full BenchmarkRunner measure
- [x] Uplift / report helpers reject approx-sourced numbers
- [x] Retest-on-change path clears arming when agreement fails
- [x] Tests cover armed vs unarmed prune and uplift refusal
- [x] Feature flag defaults off; disarm is automatic on retest fail
