# V24 Architecture Upgrade Map

This map records the v24 upgrades requested in the architecture ledger
(eighteenth period, items #103–#108). The additions are opt-in libraries:
two Rust modules in `uc-engine`, four Python components, and no new
service, database migration, scheduler, container runtime, or live route.
Every feature flag defaults off.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Three-tier KV residency | `crates/uc-engine/src/triple_kv_manager.rs` | n/a (library) | KV cache planning; no engine caller yet | module only |
| Task-adaptive code views | `python/ultimate_coders/search/adaptive_code_view.py` | `UC_ADAPTIVE_CODE_VIEW` | context assembly; no worker caller yet | module only |
| Prompt-injection relative fence | `crates/uc-engine/src/planters_fence.rs` | n/a (library) | tool authorisation; no engine caller yet | module only |
| Adversarial speculative reasoning | `python/ultimate_coders/agent/adv_spec_reasoner.py` | `UC_ADV_SPEC_REASONER` | model draft scheduling; no caller yet | module only |
| Differential bisect fuzzing | `python/ultimate_coders/acceptance/diff_bisect_fuzzer.py` | `UC_DIFF_BISECT_FUZZER` | acceptance fuzzing; no caller yet | module only |
| Latent-space context grafting | `python/ultimate_coders/search/cache_graft.py` | `UC_CACHE_GRAFT` | context and KV planning; no caller yet | module only |

## Three-tier KV residency (#103)

`triple_kv_manager.rs` models a GPU → DRAM → SSD block hierarchy. Each
`access` reports the hit tier, reused suffix tokens, recomputed tokens,
evictions, and whether the block was refilled from SSD. GPU overflow is
demoted oldest-first to DRAM, DRAM overflow to SSD, and SSD overflow is
dropped. A hit below GPU is promoted back to GPU while preserving the
suffix that was already cached. The module is an in-memory planner; it does
not allocate or move real KV blocks.

## Task-adaptive code views (#104)

`adaptive_code_view.py` routes a task to one of five code representations:
AST, call graph, dependency view, documentation, or a hybrid view. The
router selects the task's preferred views, scores candidates by lexical and
symbol relevance, and trims to a token budget. When the preferred views do
not fit, it degrades deterministically to the smallest useful view rather
than dropping all context.

## Prompt-injection relative fence (#105)

`planters_fence.rs` models the PlanTERS relative-safety fence. A first read
of untrusted external content permanently taints the session and revokes
every injectable or side-effect tool; read-only tools remain available.
Privileged calls are denied unless a human-review request for that specific
tool has been approved, and review requests are bounded by a deterministic
approval budget. The invariant is monotone: once tainted, the session never
returns to an untainted state.

## Adversarial speculative reasoning (#106)

`adv_spec_reasoner.py` plans restarts for critical reasoning forks. A
`DraftModel` exposes only the small trainable fraction used by the
adversarial draft (0.06–0.28% of parameters). `should_restart` requires a
critical fork with both low confidence and high entropy; `plan` emits the
restart decisions and aggregate density. The module does not train or call
a model; it only computes the restart policy.

## Differential bisect fuzzing (#107)

`diff_bisect_fuzzer.py` builds a `ComputationGraph`, topologically orders
it, and bisects a failing graph to the first divergent node. `EmbersMutator`
generates defect-guided mutations and `FuzzCampaign` records pass/fail
triage across cases. The module is a deterministic test-planning helper and
does not execute workloads or collect real compiler traces.

## Latent-space context grafting (#108)

`cache_graft.py` plans latent document grafts. `CacheGraftPlanner.graft`
selects documents under a token budget and reports projected memory,
throughput, and TTFT metrics. `offload_layers` sorts lookup-table layers by
prefetch deadline and offloads them until the host-memory target is met,
reporting the offloaded layer ids and deadlines. The planner does not read
or write real KV cache or NVMe devices.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v24 -q --no-cov
# 40 passed, 4382 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/search/adaptive_code_view.py \
  python/ultimate_coders/agent/adv_spec_reasoner.py \
  python/ultimate_coders/acceptance/diff_bisect_fuzzer.py \
  python/ultimate_coders/search/cache_graft.py \
  tests/python/test_v24_*.py
# All checks passed

cd packages/uc-orchestrator && \
  bun test src/rl src/skills src/harvest src/harness src/telemetry
# 192 pass, 0 fail

cargo test -p uc-engine --lib
# 860 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory. `cargo fmt -p uc-engine -- --check`
is clean.

## Non-goals

- `triple_kv_manager` does not allocate or move real KV blocks.
- `adaptive_code_view` does not fetch files; callers provide candidates.
- `planters_fence` is not an OS sandbox or a real human-review service.
- `adv_spec_reasoner` does not train or serve a model.
- `diff_bisect_fuzzer` does not execute workloads or read compiler logs.
- `cache_graft` does not touch real KV cache or NVMe hardware.
