# Agent Trust / Eval — P1 Spec

Status: **ready-for-agent** (planning artifact; no product code in this change)
Authority: [D4](issues/D4-approx-eval-enablement-gate.md), [D5](issues/D5-failure-type-reviewers.md), [D6](issues/D6-hy-memory-wiring.md); inherits P0 identity from [D1](issues/D1-three-benchmark-split.md)
Map: [map.md](map.md)
Depends on: P0 tickets 01–06 implemented (`eval_role`, report-only uplift, independent re-eval / dual-gate, `completion_conditions` + adapters)

## Problem Statement

P0 made select vs report identity, dual correctness, and non-LLM completion enforceable behind flags. Three P1 gaps remain: (1) there is still no rank-agreement-gated approximate scorer, so operators cannot safely prune candidates before full Oracle without risking uplift laundering; (2) review is still a JSON format gate, while failure routing taxonomy already exists but is not a calibrated per-type reviewer; (3) Hy-Memory stays MCP-optional with no UC-enforced supersede-not-delete on write and no opt-in auto context inject. Until those land, approx fast-path, typed review, and cognitive memory stay aspirational relative to decided D4–D6.

## Solution

Ship P1 as three coordinated product seams, all behind feature flags that default off:

1. **Approx-eval enablement gate (D4).** Add a rank-agreement harness over a fixed **report**-role hold-out truth set (Spearman and/or Kendall — implementation tickets lock the metric). Only after the gate passes may `UC_APPROX_EVAL_ENFORCE` arm a prune-only scorer. Approx scores never feed Oracle accept or public uplift. Bench/code change forces retest; failure auto-disarms the approx path.

2. **Failure-type reviewers (D5).** Align reviewer kinds with the existing `FailureKind` taxonomy in routing (`transient` / `auth` / `schema` / `unknown`). One reviewer per kind; thresholds only from cross-validation on labeled data. No labels ⇒ that kind stays closed (format gate / human). Never commit a default 0.5 as “calibrated.” Wire verified outcomes into completion evidence (`review_verified`) without letting LLM review JSON alone pass completion.

3. **Hy-Memory wiring (D6).** Keep MCP-only as default. Enforce supersede-not-delete on UC write paths regardless of auto-inject. Opt-in `UC_HY_MEMORY_AUTO_CONTEXT` may inject recall into worker context assembly. Pin package version; secrets stay in allowlisted env (never argv). Stay off / no egress until compliance review allows.

## User Stories

1. As a MetaInfer operator, I want obviously-bad candidates pruned by an armed approx scorer before full Oracle, so that search loops spend less wall time without changing accept semantics.
2. As a release owner, I want approx scores banned from public uplift and README figures, so that prune-only cannot launder selection bias.
3. As a trust engineer, I want rank agreement measured on a report-role hold-out before the approx flag can arm, so that a drift-prone proxy cannot enable itself.
4. As a CI author, I want failed re-agreement after bench/code change to auto-disarm approx, so that stale calibration cannot stay on.
5. As a review-node author, I want one reviewer per routing failure kind, so that taxonomy does not fork between retry routing and review policy.
6. As a safety reviewer, I want kinds without labeled data to stay disabled, so that uncalibrated scores cannot open gates.
7. As a completion author, I want typed-review success to set non-LLM `review_verified` evidence, so that completion adapters stay fail-closed on self-report alone.
8. As a worker operator, I want Hy-Memory to stay MCP-voluntary by default, so that deployments do not egress or inject without opt-in.
9. As a memory steward, I want writes to supersede rather than delete old facts, so that history remains auditable.
10. As a context engineer, I want optional auto-inject behind `UC_HY_MEMORY_AUTO_CONTEXT`, so that recall can enter compile_context tiers when explicitly enabled.
11. As an implementer of ticket 08, I want ticket 07’s agreement gate and arming latch, so that prune wiring cannot bypass enablement.
12. As an implementer of ticket 10, I want ticket 09’s per-kind interface, so that CV thresholds have a place to land without inventing a second taxonomy.
13. As a wayfinder reader, I want P1 tickets linked from the map as ready-for-agent, so that agents know the grabable frontier after P0.
14. As a grilling follow-up owner, I want P2 (D7–D8) left out of this spec, so that P1 stays shippable without stall replan or NetworkMode enforcement.
15. As a corpus guardian, I want planning docs to avoid inventing untracked path-shaped backticks, so that the spec-ref mention census stays clean.

## Implementation Decisions

- **Prune beside Oracle, never inside.** Approx lives as a pre-filter on candidates (workflow / MetaInfer fan-in). `Oracle.evaluate` and report remasure remain full `BenchmarkRunner` paths only.
- **Arming latch.** Human sets `UC_APPROX_EVAL_ENFORCE`; runtime also requires a passing agreement record (optional `UC_APPROX_EVAL_ARMED` latch written by the gate). Either missing ⇒ prune path inert.
- **Truth set role.** Agreement hold-out must be report-role per D1. Do not use select-tagged scores as ground truth for enabling approx.
- **No DuoDiscover dependency.** Build a small in-repo agreement + approx scorer seam; do not invent a second Oracle.
- **Shared taxonomy.** Reviewer kinds import or re-export routing `FailureKind`; do not define a parallel enum with different names.
- **Labels gate enablement.** Per-kind enablement is data-presence + CV artifact, not a hardcoded float.
- **Hy-Memory non-replacement.** Layered ShortTerm/LongTerm memory stays; Hy-Memory is cognitive extract/MCP, not a storage swap.
- **Supersede independent of auto-inject.** Write-path invariant always on when Hy-Memory writes are used; auto-context flag only controls inject.
- **Flags default off.** New enforcement never on by default; compliance-sensitive egress stays off until review.

## Testing Decisions

Good tests assert observable prune / review / memory behaviour at module boundaries—not private helpers or file layout.

1. **Seam D — approx agreement + prune.**  
   - Agreement fails ⇒ approx cannot prune.  
   - Agreement passes + enforce on ⇒ prune may drop candidates; survivors still need full Oracle.  
   - Approx scores never appear in public uplift helpers.  
   - Retest failure clears arming.

2. **Seam E — typed reviewers.**  
   - Each `FailureKind` has an isolated reviewer entrypoint.  
   - Missing labels ⇒ kind disabled; format gate still works.  
   - No default 0.5 threshold in-repo as calibrated.  
   - Successful typed review can populate `review_verified` for completion; JSON `approved` alone cannot.

3. **Seam F — Hy-Memory write + optional inject.**  
   - Default: no worker auto-inject.  
   - Flag on: recall appears in context assembly when package available; missing package fails soft (no crash).  
   - Remember path marks superseded facts rather than deleting.  
   - Secrets never on argv.

Prefer one vertical demo test per ticket plus focused unit tests on the three seams.

## Out of Scope

- P2 decisions D7–D8 (stall replan ownership, sandbox NetworkMode / Landlock).
- Strict three-way search/select/report **dataset** split (D1 P1 follow-on; may open a later ticket once workloads are authored separately). P1 approx truth set uses report-**role** hold-out under existing P0 remasure identity.
- Project ontology via MCP; README numeric recomputation CI.
- Publishing GitHub wayfinder issues or `wayfinder:*` labels without explicit authorization.
- Replacing layered Memory with Hy-Memory.
- Calibrating thresholds without labeled data.

## Further Notes

- Feature flags for P1 enforcement stay **off by default**.
- Decision authority remains D4–D6; this spec and tickets 07–12 are the implementation backlog linked from [map.md](map.md).
- Vertical slices and blockers: T7 none → T8←T7; T9 none → T10←T9; T11 none; T12 none (compliance note). Frontier after publish: T7, T9, T11, T12.
