# P2 runtime policy baseline

## Goal

Continue architecture map #656 with a runnable, conservative first version of
optimizer diagnostics, explicit review verdicts, and capacity-based placement.
The user requested continued design and implementation on 2026-09-20 after
being informed that the original external proposal is unavailable. This is a
new repository-grounded design, not a reconstruction of that proposal.

## Requirements

1. Read one durable graph's attempt metrics through a read-only PostgreSQL
   transaction and expose a JSON CLI report. No migrations or graph writes.
2. Define versioned metrics: useful work is successful non-review terminal
   attempt time / all terminal attempt time; coordination is review terminal
   attempt time / all terminal attempt time; activation inflation is all
   attempts / distinct activated nodes. These are operational proxies, not
   quality scores. Missing timings invalidate time ratios; zero denominators
   yield null. Usage totals cover reported successful events only and expose
   coverage separately; per-step usage must not be counted twice.
3. Explicit `review` capability requires a strict JSON verdict from the sandbox.
   Rejected, malformed, execution-failed or file-modifying reviews cannot
   succeed. Ordinary tasks preserve existing behavior. Existing gateway retry
   limits apply; no automatic repair graph is introduced.
4. Add opt-in `capacity` placement beside default `affinity`. Capacity ranks
   by load/capacity, then affinity, locality and worker ID; zero overlap is
   eligible. Existing capability/scope/version/independence gates stay intact.
   No eligible dedicated worker still uses existing shared-queue fallback.
5. Document configuration, failure modes and the existing shared-queue review
   independence limitation without claiming it has been eliminated.

## Acceptance

- A worked metrics fixture gives useful=0.6, coordination=0.2, activation=1.5;
  unknown timing and empty populations return null rather than fabricated zero.
- Report queries bind graph ID, share a read-only repeatable-read snapshot,
  and return NotFound for absent graphs; the CLI requires a database URL.
- Sandbox execution tests prove verdicts reach SubtaskResult and rejection
  cannot turn into success; non-review text is unaffected.
- Placement tests prove capacity can select a zero-affinity worker, exact load
  ordering, deterministic ties, gate preservation and unchanged default mode.
- Relevant Rust/Python checks and independent standards/spec review pass.

## Excluded

Automatic tuning, price auctions, multi-tenant quotas, repair-loop generation,
new public RPCs/UI, automatic review-node insertion, and a new delivery protocol.
Those require separate implementation tickets; this task is one baseline slice.

## Test seams

Public metrics calculation and report reader; Worker sandbox-result boundary;
placement policy and WorkerRegistry candidate boundary. Baseline: 9be830d.
