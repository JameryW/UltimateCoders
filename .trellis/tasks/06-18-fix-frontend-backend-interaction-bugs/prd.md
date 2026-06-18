# Fix Frontend-Backend Interaction Bugs

## Goal

Fix logic bugs and UX issues in the frontend-backend interaction layer across Dashboard, TUI, and Python Engine.

## Requirements

### P0 — Must fix
1. **Event dedup** — Replace timestamp-based dedup with source-agnostic key (`task_id:subtask_id:type:seq`), add source tracking to prevent SSE+gRPC double-processing
2. **Dashboard task ops channel** — Pause/resume use gRPC-Web when connected, REST as fallback
3. **gRPC-Web retry exhaustion UX** — Show persistent reconnect banner after max retries, not just red indicator
4. **TUI stream rebuild** — Don't reset events array on activeTaskId change; switch watchTask stream without clearing history

### P1 — Should fix
5. **Share gRPC-Web transport** — Extract shared transport from useGrpcWeb, use in SearchPanel
6. **TUI Ctrl+P edge cases** — Check task status (not subtask status) for pause/resume decision
7. **Dashboard loading flicker** — Skip REST fetchInitial for data already populated by gRPC-Web

### P2 — Nice to have
8. **Python Engine getattr safety** — Catch AttributeError in fallback wrapper
9. **Dashboard TaskSubmitForm fallback** — Show REST submit option when gRPC disconnected

## Acceptance Criteria

- [ ] No duplicate events in Dashboard when both SSE and gRPC-Web emit same event
- [ ] Dashboard pause/resume works via gRPC-Web when connected
- [ ] gRPC-Web retry exhaustion shows user-visible reconnect action
- [ ] TUI stream doesn't lose event history on activeTaskId change
- [ ] SearchPanel uses shared gRPC-Web transport
- [ ] TUI Ctrl+P checks task status correctly
- [ ] Python Engine fallback catches AttributeError
- [ ] TypeScript compiles, no regressions

## Technical Approach

### 1. Event dedup (Dashboard)
- Add `source` field to dedup key: `${ev.task_id}:${ev.subtask_id ?? ''}:${ev.type}:${ev.source ?? 'unknown'}`
- Track last-processed event per `task_id:type` pair; if same type arrives within 500ms window, skip
- Alternatively: maintain a Set of `task_id:subtask_id:type` and only dedup within the same source. Since SSE and gRPC-Web can both legitimately emit the same event, the real fix is to process from one source at a time with the other as fallback.

### 2. Dashboard task ops via gRPC-Web
- Add `pauseTask`/`resumeTask` methods to `useGrpcWeb` hook
- In App.tsx, prefer gRPC-Web calls, fall back to REST API

### 3. gRPC retry exhaustion UX
- After MAX_RETRY, set state to "exhausted" (new GrpcConnectionState)
- Show persistent banner with "Reconnect" button
- Same for TUI useTaskEvents

### 4. TUI stream rebuild
- In useTaskEvents, when activeTaskId changes, cancel old stream and start new one WITHOUT clearing events/subtaskMap
- Only clear on explicit `clearTask()` call

### 5. Shared transport
- Extract transport creation from useGrpcWeb into a module-level singleton (like SearchPanel already does)
- SearchPanel imports from useGrpcWeb or a shared `grpc-transport.ts` module

### 6. TUI Ctrl+P fix
- Check `task?.status` instead of subtask status for pause/resume decision
- Only allow pause on in_progress/planning tasks, resume on paused tasks

### 7. Loading flicker
- In App.tsx, conditionally skip REST fetchInitial endpoints when gRPC-Web is already connected and has data

### 8. Python getattr safety
- Wrap `getattr(...)(...)` in try/except that catches AttributeError + TypeError

## Out of Scope

- Python watch_task redesign
- Full Dashboard offline mode
- TUI gRPC native client refactor
- Proto/timestamp unit changes (confirmed seconds is correct)

## Technical Notes

- Proto `TaskProto.created_at/updated_at` = int64 (Unix seconds, confirmed via `conversions.rs:665`)
- Proto `TaskEvent.timestamp` = string (ISO 8601 / RFC 3339)
- SSE `timestamp` = Python `datetime.now(timezone.utc).isoformat()` (also ISO 8601)
- Dashboard uses `@connectrpc/connect-web` (gRPC-Web transport)
- TUI uses `@grpc/grpc-js` (native gRPC, different transport)
- Can share transport within Dashboard but not between Dashboard and TUI
