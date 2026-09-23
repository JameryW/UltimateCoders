# Repair red main after push (2026-09-23)

## Goal

main green again: Journal CI + Scripts CI pass on the pushed content, with
every pin move accounted per-row (T41–T44 discipline — no blind number
bumps).

## Requirements

1. journal-3.md sessions 52–55: delete the `add_session.py` skeleton head
   (heading + Date/Task/Branch + Summary + bare `### Main Changes`) and
   skeleton tail (`### Git Commits` with `(see git log)` rows,
   `- [OK] (Add test results)`, `- None - task complete` block) around
   each full entry. Keep the full entries. Result: one `## Session N`
   each, 6 headings each, zero placeholder lines, session numbers 52–55
   unique, index count 55/55 with no edit to index.md needed (verify).
2. `durable-runtime-p2-recon.md:38`: repair the stale
   `worker_service.rs` `dispatch_gate` pointer to the post-P2 line (def at
   326 — verify the anchor, then point at the correct line per the guard's
   rule, same as T42/T43 repairs).
3. `test_check_spec_refs.py`: move pins 271 → 275 (mentions) and
   OK 114 → 113 / stale 7 → 8, each with the per-row account from
   research/notes.md in comments. Refs count unchanged (130).
4. `test_check_tasks_refs.py`: move pin (804, 805) → re-derived pair from
   per-file accounting of the four archived task dirs (same reachable-pair
   method as T44; verify the delta is exactly those dirs, no stray).
5. Verify locally: `check-spec-refs.py` audit, `check-tasks-refs.py`,
   `check-journal-ledger.py`, `check-line-endings.py`, plus the three
   pinned pytest files. If line-endings moved, account per-file (T44
   method) — do not silently bump.
6. Python CI status on the pushed head: re-check; if it failed, triage
   (may be fallout of the same push).

## Acceptance

- The four failing CI tests pass locally with pins moved and accounted.
- `git diff` touches only: journal-3.md (deletions), recon doc (1 pointer),
  the two pin test files (pins + comments).
- Commit + push (CI is the only verifier for the red state); Journal and
  Scripts CI green on the new head.

## Out of scope

- `add_session.py` behavior change (filed as lesson in session notes;
  future `--content-file` calls pass Main-Changes-only bodies).
- T19 race / P2-3 market (still pending §21).

## Decisions

- Pin moves follow T41–T44 precedent (accounted moves in the repair
  commit). No user-owned decision exists.

## Open questions

None.
