# Spec P7 — Local tool receipts on the Decision Record (evidence surface)

Blocked by: D17 (decided). Builds on the P0 Decision Record (D5).

## Goal

Close the remaining "broad tool receipts thin" gap for local effects without a new
pipeline: an attempt's changed-file set and tool names become part of its
attempt-scoped Decision Record, derived from the git diff the sandbox already
computes.

## What to build

1. **Compaction helper** in `decision_record.py`:
   `workspace_summary(modified_files, tool_calls)` →
   `{files: [{path, change_type}], file_count, tool_calls, truncated}`.
   - Accepts `FileChange`-like objects or dicts; sorts files by path, tool names
     de-duplicated in first-seen order.
   - Caps: 50 files, 20 tool names; `truncated` is true when either cap clipped.
   - **Never** copies `diff`/content; a missing/blank path is dropped.
2. **`record_terminal(..., workspace=None)`**: when a summary is supplied it is stored
   under `terminal["workspace"]`; absent workspace leaves the terminal block unchanged.
3. **Worker terminal write** (`_record_decision_terminal`): pass
   `workspace_summary(result.modified_files, result.recent_tool_calls)`; best-effort
   like the existing start/terminal writes.
4. **Docs**: V8 map (P7 + D17), agent-os §5 row, assessment note, D16 cross-link.

## Acceptance criteria

- [ ] A success record carries `terminal.workspace.files` with path + change type and
      `file_count`.
- [ ] Tool names are recorded, de-duplicated, and capped at 20.
- [ ] Files are capped at 50 and sorted; `truncated` reports a clipped list.
- [ ] No `diff`/content text appears anywhere in the stored record.
- [ ] A blank/missing path is dropped instead of stored as an empty row.
- [ ] `record_terminal` without a workspace keeps the old terminal shape.
- [ ] Persistence failure still never fails the subtask.

## Out of scope

- Per-tool interception (MCP `write_file`, `uc-fs` stdio)
- `preserve_effects` consumption of the workspace block
- Content hashes / a new receipt store
- Dashboard route or schema changes
