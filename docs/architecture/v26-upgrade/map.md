# V26 Architecture Upgrade Map

This map records the v26 upgrades requested in the architecture ledger
(twentieth period, items #115–#120). The additions are opt-in libraries:
two Rust modules in `uc-engine`, two Python components, two TypeScript
modules, and no new service, database migration, scheduler, container
runtime, or live route. Every feature flag defaults off.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Harness refinement search | `packages/uc-orchestrator/src/rl/harness_flow_mcts.ts` | n/a (library) | harness evolution; no caller yet | module only |
| Latent tool-observation compression | `python/ultimate_coders/search/coca_latent_ctx.py` | `UC_COCA_LATENT_CTX` | context assembly; no caller yet | module only |
| Active-window note compactor | `python/ultimate_coders/agent/memento_compactor.py` | `UC_MEMENTO_COMPACTOR` | long-context compaction; no caller yet | module only |
| Three-group tree credit | `packages/uc-orchestrator/src/rl/trigrpo_tree.ts` | n/a (library) | RL export; no caller yet | module only |
| Cross-source compositional-harm guard | `crates/uc-engine/src/seam_compositional_guard.rs` | n/a (library) | tool dispatch authorization; no engine caller yet | module only |
| Pooled prover and curiosity | `crates/uc-engine/src/prover_pool.rs` | n/a (library) | verification and investigation scheduling; no engine caller yet | module only |

## Harness refinement search (#115)

`harness_flow_mcts.ts` implements a UCT refinement tree over complete
executable harnesses. A `HarnessFlowMcts` search evaluates the root, selects
an expandable node by the UCT score, asks the caller's `branch` function for
one new harness per iteration, and backpropagates the average evaluation.
Score ties are broken by lexicographic harness name. The module does not
execute harnesses or invoke a model.

## Latent tool-observation compression (#116)

`coca_latent_ctx.py` compresses a tool observation into deterministic
FNV-1a latent tokens. A `LatentObservation` reports the latent and source
token counts, compression ratio, and a bounded preview. `ActionDistribution`
validates a normalised next-action distribution, and the compressor exposes
KL divergence and an action-preservation test so callers can verify that the
latent representation keeps the next action. The module does not train a
model or call an embedding service.

## Active-window note compactor (#117)

`memento_compactor.py` keeps a strict active window of recent entries and
folds the oldest entries into a compact note whose token count is a quarter
of the folded input. `needs_compaction` triggers on the window budget;
`compact_if_needed` returns a no-op while under budget. `traceback` returns
an earlier high-confidence note when the latest note falls below the
confidence threshold, and `render_active` renders the current window under a
character bound. The module does not summarise with a language model.

## Three-group tree credit (#118)

`trigrpo_tree.ts` builds a trajectory tree and computes three complementary
advantages: a root group standardised within the prompt, a node group
standardised against parent siblings (root nodes score zero), and a
cross-task group using the task-family mean. The combined advantage weights
the groups as published; ids, parents, and rewards are validated before
storage. The module does not train a policy.

## Cross-source compositional-harm guard (#119)

`seam_compositional_guard.rs` retains observations from screens, tool
outputs, file content, and user instructions. A `HarmPattern` requires at
least two channels; it matches only when its entity appears in every required
channel and at least two distinct source instances are involved. Any match
produces a `PrivilegedInstruction` escalation and `authorize` returns
`CompositeHarmBlocked`, including attempts to waive the hazard through the
instruction channel. The module does not call an LLM judge.

## Pooled prover and curiosity (#120)

`prover_pool.rs` keeps bounded warm slots for Isabelle, Lean 4, and Miri and
hands them out deterministically. `CuriosityEngine.select` ranks candidate
investigations by expected information gain per unit cost, rejects zero-gain
and duplicate actions, and breaks equal scores by cost then action id. The
module does not start a prover process or execute a proof.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v26 -q --no-cov
# 22 passed, 4452 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/search/coca_latent_ctx.py \
  python/ultimate_coders/agent/memento_compactor.py \
  tests/python/test_v26_*.py
# All checks passed

cd packages/uc-orchestrator && \
  bun test src/rl src/skills src/harvest src/harness src/telemetry
# 236 pass, 0 fail

cargo test -p uc-engine --lib
# 892 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory. `cargo fmt -p uc-engine -- --check`
is clean.

## Non-goals

- `harness_flow_mcts` does not execute a harness; callers supply evaluation.
- `coca_latent_ctx` does not train a model or call an embedding service.
- `memento_compactor` does not generate notes with a language model.
- `trigrpo_tree` does not train a policy or run a sampler.
- `seam_compositional_guard` does not call an LLM judge; pattern evidence is
  deterministic.
- `prover_pool` does not start a prover process or execute a proof.
