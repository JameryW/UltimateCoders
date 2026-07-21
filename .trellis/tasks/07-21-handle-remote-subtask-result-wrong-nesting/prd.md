# handle-remote-subtask-result-wrong-nesting

## Goal

`NatsWorker._handle_remote_subtask_result` (`nats_worker.py:1879-1902`) reads
`summary`/`modified_files`/`error` from the **top level** of the parsed event
payload, but `_make_task_event_payload` (nats_worker.py:138) nests these under
`payload["data"]`. So on every remote subtask completion/failure:
- `data.get("modified_files", [])` → `[]` → remote file changes silently
  dropped for aggregation + merge arbitration.
- `data.get("summary", "")` → `""` → empty summary.
- `data.get("error", "Remote subtask failed")` → always the fallback → real
  remote failure messages lost.

The sibling `subtask_dispatch_rejected` handler (nats_worker.py:1836)
correctly reads `data.get("data", {}).get("reason")`, proving the nesting
convention.

## What I already know

- Bug site: `nats_worker.py:1879-1902` (3 read sites).
- `data` passed in = entire parsed payload (`_handle_task_event` line 1794
  `json.loads(msg.data)`).
- `_make_task_event_payload` (line 137-140): `payload["data"] = data`.
- Writer: remote worker publishes via `publish_event("subtask_completed", ...,
  data={...})` → nested.
- Convention proof: line 1836 `data.get("data", {}).get("reason")`.

## Requirements

- Read `summary`/`modified_files`/`error` from `data.get("data", {})`.
- Minimal: introduce a local `inner = data.get("data", {}) or {}` at the top
  of the completed/failed branches and read from it.

## Acceptance Criteria

- [ ] Remote subtask completion carries `modified_files` + `summary` into
  `SubtaskResult`.
- [ ] Remote subtask failure carries real `error` text.
- [ ] `ruff check` clean.
- [ ] Existing python tests green; add regression test feeding a
  nested-payload event and asserting non-empty modified_files/summary/error.

## Definition of Done

- Fix + regression test.
- PR opened + CI green + merged.

## Out of Scope

- Other Python scan findings (race conditions, aggregator base drift, etc.).
- Rust-side conversions (already fixed in PR #347).

## Technical Notes

- `python/ultimate_coders/nats_worker.py:1847-1902` (`_handle_remote_subtask_result`).
- `python/ultimate_coders/nats_worker.py:109-141` (`_make_task_event_payload`).
- `python/ultimate_coders/nats_worker.py:1836` (correct nesting reference).
