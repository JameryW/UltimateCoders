# 05: completion_conditions

**What to build:** The agent contract package exports structured `completion_conditions` beside the existing digest / registry API. Digest continues to answer only whether goal text changed. Completion evaluation is non-LLM code invoked by worker, orchestrator, or runner; a model claim of done or approved cannot alone satisfy completion. This ticket lands the home and evaluate surface; adapters and the independent-re-eval condition type are ticket 06.

**Blocked by:** None (can start immediately).

**Status:** implemented (`UC_COMPLETION_CONDITIONS_ENFORCE`; evaluate + `evidence_flag` in `ultimate_coders.agent.contract` / `completion_conditions.py`)

- [x] `completion_conditions` is a separate structure from contract digest / registry
- [x] Evaluate API is exported from the `ultimate_coders.agent.contract` package
- [x] Digest behaviour and responsibility remain goal-change detection only
- [x] Evaluate runs without an LLM; model self-report alone cannot pass
- [x] Tests cover the contract-package seam (digest vs completion namespaces)
- [x] Feature flag defaults off
