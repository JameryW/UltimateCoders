# subtask-completed-success-default-true

## Goal

`crates/uc-grpc/src/conversions.rs:1465` (in `impl From<TaskEventProto> for AgentEvent`,
`subtask_completed` branch) reads the `success` field as:

```rust
proto.data.get("success").map(|s| s == "true").unwrap_or(true)
```

Two bugs in one:
1. **Wrong comparison (high severity):** `proto.data` is `serde_json::Map<String,
   Value>`, so `s` is `&Value`. The Python worker emits `success` as a JSON
   **bool** (`worker.py:931` `"success": result.success`). `Value::Bool(true) ==
   "true"` is always **false** (serde_json's `PartialEq<&str>` only matches
   `Value::String`). So every successfully-completed subtask is reported as
   **failed** to any Rust gRPC client watching via `watch_task` (client.rs:121 →
   `AgentEvent::from(proto_event)`).
2. **Unsafe default (med):** missing key → `unwrap_or(true)` claims success.
   Fail-safe default should be `false`.

Note: the server's own NATS-ingestion path (`server.rs:2580`) uses
`json_bool_or_default` which correctly handles both `Bool` and `String` — so
only the gRPC-client `From<TaskEventProto>` path is broken.

## What I already know

- `proto.data: serde_json::Map<String, serde_json::Value>` (server.rs:117).
- Writer: `python/ultimate_coders/agent/worker.py:931` → `"success": result.success` (JSON bool).
- Reader (broken): `conversions.rs:1465`, called via `client.rs:121`.
- Reader (correct, different path): `server.rs:2580` `json_bool_or_default`.
- `json_bool_or_default` (server.rs:241-251) is the correct pattern: match
  `Value::Bool(b) => *b`, `Value::String(s) => s == "true"`, `_ => default`.

## Requirements

- Fix `conversions.rs:1465` `success` read to handle `Value::Bool`, `Value::String`,
  and missing key. Default `false` (fail-safe).
- Reuse the exact `json_bool_or_default` pattern. Either:
  - (a) move `json_bool_or_default` to a shared location + call it, OR
  - (b) inline the same match in conversions.rs.
- Prefer (b) — minimal diff, avoids cross-module visibility churn. Add a small
  local helper `fn json_bool(data, key, default)` if a second site needs it;
  else inline.

## Acceptance Criteria

- [ ] `Value::Bool(true)` → success=true (currently false).
- [ ] `Value::Bool(false)` → success=false.
- [ ] `Value::String("true")`/`"false"` → correct bool.
- [ ] missing key → false (was true).
- [ ] `cargo test -p uc-grpc` green, incl a new regression test asserting bool handling.

## Definition of Done

- Fix applied + regression test added.
- `cargo check/fmt/clippy` clean.
- PR opened + CI green + merged.

## Out of Scope

- `events.rs:371` synthetic-TaskCreated-on-parse-fail (separate task, recovery path).
- `dispatcher.rs:152` silent subtask drop (separate task).
- Other json_to_*/conversions sites (audit covered them).

## Technical Notes

- `crates/uc-grpc/src/conversions.rs:1462-1469` (From impl, subtask_completed branch).
- `crates/uc-grpc/src/server.rs:241-251` (json_bool_or_default — reference pattern).
- `crates/uc-grpc/src/client.rs:121` (caller of From<TaskEventProto>).
- `python/ultimate_coders/agent/worker.py:931` (success writer, JSON bool).
