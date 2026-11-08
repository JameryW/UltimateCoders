# 26 — Local tool receipts on the Decision Record (T26)

**What to build:** Add `workspace_summary()` + `record_terminal(..., workspace=...)` to
`python/ultimate_coders/decision_record.py`, and pass the summary from
`Worker._record_decision_terminal` (`result.modified_files` + `result.recent_tool_calls`),
per [spec-p7.md](../spec-p7.md). Audit-only, best-effort, no flag: no behavior gate and
no new store.

**Blocked by:** D17

**Status:** implemented (`python/ultimate_coders/decision_record.py`,
`python/ultimate_coders/agent/worker.py`; focused cases in
`tests/python/test_decision_record.py`)

- [x] `workspace_summary` caps at 50 files / 20 tool names, sorted, `truncated`
- [x] `record_terminal` stores `terminal.workspace` only when supplied
- [x] Worker passes the summary on its terminal write (best-effort)
- [x] Tests: `tests/python/test_decision_record.py`
