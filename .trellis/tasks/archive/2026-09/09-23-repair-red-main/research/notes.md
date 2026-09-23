# Evidence (all re-measured 2026-09-23, worktree diff 9be830df → HEAD)

Corpus JSON diff (`check-spec-refs.py --json` on a `9be830df` worktree vs
current tree, keyed on spec+line+ref):

- Mentions 271 → 275 (+4). The +4 are exactly the P2 files' new resolved
  mentions: `runtime-policy-spec.md:5,6,7` (3: p2-policy doc, worker-service
  spec, agent-capability spec) + `worker-service-spec.md:205` (`worker.py`).
  All other agent-capability-spec.md rows are line-shifted duplicates of
  existing rows (P2 added 2 pointer lines there) — net zero.
- OK 114 → 113 / stale 7 → 8. Exactly one flip:
  `durable-runtime-p2-recon.md:38` → `crates/uc-grpc/src/worker_service.rs`
  `dispatch_gate`, OK → STALE (offset +7, def now at 326). Cause: P2's
  `placement_policy` field + setter added lines above `dispatch_gate`.
  The anchor (`dispatch_gate` def) still exists — pointer line is stale,
  same class as T42/T43 pointer repairs.
- `check-tasks-refs`: 812 vs pin (804, 805). +7/+8 from the four newly
  archived task dirs (P2, live-roster, clippy-warnings, clippy-all-targets)
  whose jsonl/prd/design cite `.trellis/` paths. T44 precedent: pins move
  in the archive commit; per-file accounting required (reachable-pair
  shape — re-derive, do not guess).
- Journal ledger: sessions 52–55 each written twice by `add_session.py`
  (skeleton head + full `--content-file` body nested under Main Changes +
  skeleton tail with `- [OK] (Add test results)` / `- None - task complete`
  placeholders). `--content-file` is Main-Changes-only by design
  (`add_session.py:180-182` `{extra_content}` slot). 20 problems: 4× double
  headings, doubled subsections, 8 placeholder lines, 4 duplicated session
  numbers. Index advisory (55 vs 59) resolves itself after dedup.
- Rust CI on the pushed head: success (extended `--all-targets` clippy gate
  passed first run). Python CI was in_progress at last check — re-check in
  implementation.
- `check-line-endings`: not in the failed list; verify locally it still
  passes (new files may move its pin — if red, account per-file like T44).
