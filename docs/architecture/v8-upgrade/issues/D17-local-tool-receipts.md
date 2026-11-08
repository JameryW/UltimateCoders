# D17 — Local tool receipts live on the Decision Record

Status: decided
Decided: 2026-11-08

## Question

D16 deferred MCP `write_file` receipts to "the evidence surface" and promised its own
decision ticket. Where do **local** (in-workspace) tool/effect receipts live, and how
are they derived — per-tool interception, a new store, or the existing attempt-scoped
Decision Record?

## Decision

**Local tool receipts = attempt-scoped `workspace` evidence on the existing Decision
Record (D5), derived from the already-computed `SubtaskResult.modified_files` (git
diff) plus `recent_tool_calls`. No per-tool interception, no new namespace, no flag.**

1. **Surface.** The terminal block of a Decision Record gains
   `workspace: {files: [{path, change_type}], file_count, tool_calls, truncated}`.
2. **Derivation.** The sandbox already fills `file_changes` from
   `engine.get_changes_and_reset` (a git diff) for **every** adapter — including
   subprocess agents whose tool loop the worker cannot see. The receipt is therefore
   derived from filesystem truth, not from the agent's self-report.
3. **Explicitly not per-tool interception** (MCP `write_file` / `uc-fs` stdio):
   most adapters run their own tool loop in a subprocess and the worker sees tool
   names only; `uc-fs` holds no `RuntimeState`, so a receipt there would need a new
   out-of-band pipeline. And a per-tool row says "the agent claimed to write X" while
   the diff says "X changed" — the diff is the audit truth.
4. **Log-safe.** Path + change type only. Never `FileChange.diff`, never tool stdout
   or prompt content.
5. **Caps + determinism.** 50 files, 20 tool names, sorted; `truncated=true` when
   clipped. Same input ⇒ same record.
6. **No flag.** Audit-only and best-effort (the P0 discipline): losing or omitting a
   record must never fail the subtask, and there is no behavior gate to flip.

## Why

The open gap was "broad tool receipts still thin". What auditors actually ask is
"what did this attempt change?" — and that answer already exists in the diff the
sandbox computes for progress detection. Putting it on the attempt-scoped Decision
Record closes the gap with zero new pipeline and zero new store, and it cannot be
fabricated by model prose.

## Boundary (what this decision does not do)

- No `preserve_effects` wiring — this produces evidence; consuming it is a later ticket.
- No content hashes: the published commit already carries git blob identity; a second
  hash here would be a parallel truth.
- No dashboard schema change: the decisions endpoint returns records as-is, so the
  new block surfaces without a route change.

## Implementation ticket

T26: add the compaction helper + `record_terminal(..., workspace=...)`, pass it from
the worker's terminal write, and pin the caps / determinism / no-content rules.
Native edge T26 ← D17.
