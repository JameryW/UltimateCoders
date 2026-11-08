# T21: docs numeric manifest + pin CI

## Goal

Ship a machine-readable numeric manifest for docs-facing measured numbers (SSE latency family first) plus a hard-fail CI checker: document text and pin must match. Refuse undocumented uplift percentages on README-facing surfaces unless the entry is on the manifest with `eval_role=report` provenance. Must run without a live cluster.

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/21-docs-numeric-pin-ci.md`
- Decision: [D10](../../docs/architecture/agent-trust-eval/issues/D10-readme-numeric-ci.md)
- Spec: [spec-p3.md](../../docs/architecture/agent-trust-eval/spec-p3.md) Seam J
- Peer guard: `scripts/check-readme-ci-table.py` + `.github/workflows/ci-readme-ci-table.yml`
- Live remeasure (deferred): ticket 22 / `scripts/verify-sse-latency.py`

## What I already know

* D10 MVP = manifest + hard-fail pin checker; SSE family first; live remeasure deferred.
* Documented SSE numbers today: `6.394ms` / `55.712ms` (steady/burst max) in EN+ZH README and `docs/live-feedback-and-metainfer-verification.md`; verification doc also pins `2.260ms` / `55.270ms` p99 and the `200ms` ceiling.
* README.md / README.zh-CN.md currently contain **zero** `%` tokens — uplift ban starts empty-allowlist / fail-closed.
* Peer pattern: dedicated path-filtered workflow + sandbox mutation tests; updating workflow count requires README prose + table (Eleven→Twelve / 十一→十二).
* No live stack may be required for green.

## Assumptions (temporary)

* Manifest path: `docs/numeric-pins.yaml` (YAML; PyYAML already used by peer CI).
* Checker: `scripts/check-docs-numeric-pins.py` (exit code IS the verdict).
* Uplift ban surfaces: `README.md`, `README.zh-CN.md` only for MVP.
* Pin match = each pin’s `must_contain` substring must appear in each listed surface file; drift either way (doc change without pin / pin without doc) fails.
* `uplift_allowed` entries require `eval_role: report` provenance; empty list means any `\d+(\.\d+)?%` on ban surfaces fails.

## Research References

* [`research/grounding.md`](research/grounding.md) — SSE pin inventory, peer CI shape, Approach A sketch, test plan

## Research Notes

### Constraints from our repo/project

* Closed input set → own path-filtered workflow (same rationale as `ci-readme-ci-table.yml`).
* Adding a workflow forces README CI table + prose count updates.
* T22 owns live remeasure; this ticket must never invoke `verify-sse-latency.py` against a stack.
* Align D1/T2: uplift only with report-role provenance.

### Feasible approaches here

**Approach A: YAML manifest + dedicated checker + path-filtered CI** (Recommended)

* How: `docs/numeric-pins.yaml` lists SSE pins + uplift allowlist; `check-docs-numeric-pins.py` hard-fails on drift / undocumented `%`; own workflow mirrors README CI Table CI.
* Pros: Matches D10; peer-pattern reuse; offline green; clear T22 seam.
* Cons: Manifest is a second hand-written surface (mitigated by the checker itself).

**Approach B: scrape numbers from verification JSON evidence only**

* How: Read `.trellis/tasks/.../live-verification.json` and assert README quotes those values.
* Pros: Single evidence source.
* Cons: Couples docs CI to an archived task path; harder uplift ban; D10 asked for an explicit manifest.

**Approach C: extend `check-readme-ci-table.py`**

* How: Fold numeric pins into the existing README guard.
* Pros: One less workflow.
* Cons: Mixes unrelated judgments; path filter would either over- or under-fire; harder ablation.

## Decision (ADR-lite)

**Context**: T21 must hard-fail docs↔pin drift for SSE family and ban undocumented README uplift `%` without a live cluster.  
**Decision**: **Approach A** — locked via「继续」.  
**Consequences**: New manifest + checker + workflow; README workflow count 11→12; T22 remains deferred live remeasure.

## Requirements (evolving)

* Add `docs/numeric-pins.yaml` covering at least the SSE latency family used in README + verification doc.
* Add `scripts/check-docs-numeric-pins.py`: hard-fail on pin↔doc mismatch; hard-fail undocumented uplift `%` on README surfaces; no live stack.
* Wire `.github/workflows/ci-docs-numeric-pins.yml` (path-filtered) + update README CI table / prose counts.
* Tests cover match (repo green), drift, and uplift-ban cases via sandbox mutations.
* Update map + ticket 21 status when landed.
* Do not run live SSE remeasure; do not invent T18/T22 scope.
