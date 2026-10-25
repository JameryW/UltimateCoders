# 07: approx rank-agreement gate

**What to build:** A rank-agreement harness over a fixed report-role hold-out truth set (Spearman and/or Kendall — lock the metric in the PR). The harness records pass/fail and may write an arming latch. `UC_APPROX_EVAL_ENFORCE` alone must not prune until agreement has passed. No candidate prune wiring yet (ticket 08). Do not invent a second Oracle or feed approx scores into uplift.

**Blocked by:** None (uses P0 report-role identity / remasure; full dataset three-way split is out of scope).

**Status:** implemented (branch `cursor/t7-approx-rank-agreement-7c92`; flags `UC_APPROX_EVAL_ENFORCE` / `UC_APPROX_EVAL_ARMED` default off; Spearman ρ threshold 0.8)

- [x] Agreement API evaluates approx ranking vs full Oracle ranking on a report-role hold-out
- [x] Metric choice (Spearman) and threshold are explicit in code + tests
- [x] Failed or missing agreement leaves approx unarmed
- [x] Passing agreement can set an arming latch consulted by later prune wiring
- [x] Tests cover the agreement seam without requiring MetaInfer GPU
- [x] Feature flag(s) default off (`UC_APPROX_EVAL_ENFORCE`; optional latch env)
