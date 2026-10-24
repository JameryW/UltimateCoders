# Agent Trust / Eval — P0 Spec

Status: **ready-for-agent** (planning artifact; no product code in this change)
Authority: [D1](issues/D1-three-benchmark-split.md), [D2](issues/D2-independent-correctness-boundary.md), [D3](issues/D3-completion-conditions-home.md)
Map: [map.md](map.md)
Assessment: [agent-trust-eval-assessment.md](../agent-trust-eval-assessment.md)

## Problem Statement

Operators and reviewers cannot trust Agent-reported uplift or “done” claims. The same benchmark workload today both guides search/selection and fills public improvement numbers, so reported gains can be selection-biased. Correctness still leans on harness self-report from Agent-writable space, so a crafted or optimistic harness can pass the Oracle gate. Completion is scattered across verify commands, step conditions, and review JSON, with no single structured, non-LLM completion authority beside the contract digest. Until select vs hold-out report, independent correctness re-eval, and machine-checked completion conditions land, Agent Trust / Eval remains partly aspirational even where fingerprints and Oracle hard gates already exist.

## Solution

Ship P0 as three coordinated product seams, all behind feature flags that default off:

1. **Select vs report split (D1).** Every evaluation product carries an `eval_role` of `search`, `select`, or `report`. Select and report workloads are disjoint. The UC Oracle accept gate may use select; every public uplift, README figure, and release artifact must come only from a protected remeasure on the hold-out report set. Search may share the select development set in P0, but search/select scores must never be labeled or published as report.

2. **Dual correctness gate (D2).** Keep protected harness fingerprint / provenance attestation. Before accept, run correctness again on a path the Agent cannot write. Agent-generated harness output may inform candidates but cannot alone close the correctness gate.

3. **Structured completion conditions (D3).** Extend the agent contract package with a `completion_conditions` namespace beside (not inside) the digest / registry that answers “did the goal change?”. Evaluate those conditions with non-LLM code in worker, orchestrator, or runner. Adapt existing verify-command and step-condition shapes into condition types under that single authority, including a condition that independent correctness re-eval passed.

## User Stories

1. As a release owner, I want public uplift numbers to come only from a hold-out report set, so that selection bias cannot inflate published gains.
2. As a MetaInfer operator, I want internal ranking scores tagged as search or select, so that I never paste them into release notes as report uplift.
3. As a UC Oracle consumer, I want the accept gate to use the select role explicitly, so that accept and publish are different decisions.
4. As a benchmark curator, I want every `BenchmarkResult` and `OracleVerdict` to carry `eval_role`, so that role mistakes are visible in logs and artifacts.
5. As a CI author, I want mixing select identity into a report field to fail closed, so that accidental mislabeling cannot ship.
6. As a security reviewer, I want Agent workspace self-reported correctness rejected as sole evidence, so that a writable harness cannot open the accept gate alone.
7. As an Oracle maintainer, I want fingerprint attestation retained, so that unprotected runners still fail closed.
8. As an Oracle maintainer, I want an independent correctness re-run outside Agent-writable trees before accept, so that F8’s remaining gap closes in product policy.
9. As a harness author, I want Agent-generated correctness harnesses usable only as candidate input, so that independent re-eval remains authoritative.
10. As a platform engineer, I want the independent re-eval path feature-flagged default-on for the dual gate once enabled, so that turning the flag on means re-eval is required, not optional.
11. As a task author, I want structured `completion_conditions` next to the contract digest API, so that “done” is machine-checkable without forking a second completion authority.
12. As a worker implementer, I want completion evaluation to be non-LLM code, so that model self-reports of done or approved cannot alone pass.
13. As an orchestrator author, I want verify-command and step-condition shapes available as condition types, so that existing workflows converge instead of growing a parallel “done” stack.
14. As a trust engineer, I want a completion condition that means “independent correctness re-eval passed,” so that D2 and D3 compose on one accept path.
15. As a dashboard user, I want accept events to show both attestation and independent re-eval outcomes, so that dual-gate failures are diagnosable.
16. As a docs maintainer, I want README and marketing uplift banned from citing select or search scores, so that documentation cannot launder selection bias.
17. As a feature-flag owner, I want all new P0 enforcement paths off by default until explicitly enabled, so that existing deployments stay behavior-compatible until opted in.
18. As an implementer of ticket 02, I want ticket 01’s `eval_role` identity rules already landed, so that report-only uplift can refuse select-tagged inputs.
19. As an implementer of ticket 04, I want ticket 03’s independent re-eval available, so that dual-gate accept can call it.
20. As an implementer of ticket 06, I want tickets 05 and 03 done, so that adapters and the re-eval condition type have both a home and a callee.
21. As a wayfinder reader, I want P0 tickets linked from the map with ready-for-agent status, so that agents know what is grabable versus still undecided for P1/P2.
22. As a grilling follow-up owner, I want P1 (D4–D6) and P2 (D7–D8) left out of this spec, so that P0 stays shippable without approx-eval, reviewers, Hy-Memory wiring, stall replan, or sandbox isolation.
23. As a corpus guardian, I want planning docs to avoid inventing untracked path-shaped backticks, so that the spec-ref mention census stays clean.
24. As an agent taking a vertical slice, I want each ticket demoable on its own once blockers clear, so that I do not need the whole P0 graph in one context window.

## Implementation Decisions

- **Roles on results.** `BenchmarkResult` and `OracleVerdict` (and any accept/report DTO that surfaces uplift) gain a required `eval_role` in `{search, select, report}`. Identity rules: a result minted under select must not satisfy APIs that demand report; publishing helpers refuse non-report roles.
- **P0 split depth.** Select and report workloads are mandatory-disjoint. Search may reuse the select development set in P0 but must still emit `eval_role=search` when used for search feedback. Strict three-way dataset split is deferred to P1 under D1.
- **Accept vs publish.** `Oracle.evaluate` / accept uses select-role evidence. Public uplift and release figures are produced only by a protected remeasure with `eval_role=report`. MetaInfer internal ranking must not write public uplift fields.
- **Dual gate.** Accept requires (a) existing fail-closed harness fingerprint / provenance attestation and (b) independent correctness re-eval whose inputs and runner live outside Agent-writable trees. Agent harness output alone is insufficient.
- **Independent re-eval ownership.** Prefer extending `BenchmarkRunner` protect/measure flows so re-eval is a first-class, protected measurement—not a trust of Agent-side callback results.
- **Completion package shape.** Keep digest / registry answering only “did the goal text change?” Add a separate `completion_conditions` structure and evaluate API exported from the `ultimate_coders.agent.contract` package (same product home as today’s digest, different namespace).
- **Non-LLM evaluate.** Worker, orchestrator, or runner evaluate conditions; LLM tokens claiming done/approved are never sole success.
- **Adapters, not a second authority.** verify-command, step-condition, and review-node approval signals become condition types or adapters under `completion_conditions`. No parallel completion registry.
- **Composition condition.** At least one condition type encodes “independent correctness re-eval passed,” binding D2 into D3.
- **Flags.** All new enforcement defaults off; enabling a flag must not weaken existing fingerprint fail-closed behavior.

## Testing Decisions

Good tests assert observable accept/report/completion behavior at module boundaries—not private helpers or file layout.

1. **Seam A — Oracle / `BenchmarkResult` `eval_role`.**  
   - Construct results and verdicts with each role; assert select≠report identity (report-only APIs reject select/search; accept may take select).  
   - Assert public uplift helpers ignore or error on non-report roles.  
   - Prior art: existing Oracle and benchmark result tests under the inference suite.

2. **Seam B — `BenchmarkRunner` protect + independent re-eval.**  
   - With the dual-gate flag on: attestation failure still rejects; attestation success without independent re-eval rejects; both passing accepts.  
   - Prove an Agent-writable harness self-report cannot alone pass when re-eval fails or is skipped.  
   - Prior art: protected-path / fingerprint fail-closed tests on the benchmark runner.

3. **Seam C — agent.contract `completion_conditions`.**  
   - Digest APIs remain unchanged in responsibility (goal-change only).  
   - Structured conditions evaluate without an LLM; model “done” alone fails.  
   - Adapter types for verify-command and step-condition round-trip; re-eval-passed condition reflects Seam B outcomes.  
   - Prior art: contract digest / registry tests beside the agent contract package.

Prefer one vertical test per ticket that demos the end-to-end behaviour that ticket claims, plus focused unit tests on the three seams above.

## Out of Scope

- P1 decisions D4–D6 (approx-eval enablement gate, failure-type reviewers / CV thresholds, Hy-Memory wiring).
- P2 decisions D7–D8 (stall replan ownership, sandbox `NetworkMode` / Landlock isolation).
- Project ontology exposed via MCP.
- README / docs numeric recomputation CI.
- Publishing GitHub wayfinder issues or `wayfinder:*` labels (docs tree remains authoritative until explicitly authorized).
- Strict three-way search/select/report dataset split (P1 follow-on under D1).
- Fully custom golden verifiers beyond the independent re-eval path (may stage later; P0 requires the dual gate, not a new verifier framework).

## Further Notes

- Feature flags for P0 enforcement stay **off by default**; fingerprint fail-closed paths already in tree must not regress when flags are off.
- Decision authority remains D1–D3; this spec and [tickets/01](tickets/01-eval-role-on-results.md)–[06](tickets/06-adapters-and-reeval-condition.md) are the implementation backlog linked from [map.md](map.md).
- Vertical slices and blockers: T1 none → T2←T1; T3 none → T4←T3; T5 none → T6←T5,T3. Frontier after publish: T1, T3, T5.
