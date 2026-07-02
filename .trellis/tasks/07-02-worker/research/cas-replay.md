# Research: Versioned/Timestamped CAS Replay for Memory Writes During gRPC Fallback

- **Query**: How to implement versioned/timestamped CAS (compare-and-set) replay for memory writes that happened during a gRPC fallback window, with last-writer-wins conflict resolution
- **Scope**: mixed (internal codebase analysis + external distributed systems knowledge)
- **Date**: 2026-07-02

## Current Architecture (Internal Findings)

### The Fallback Gap

The Python `Engine` class (`python/ultimate_coders/engine.py:122-265`) implements `_try_grpc_with_fallback` / `_try_grpc_with_fallback_async`. When `mode="grpc"` and `fallback_mode="auto"`:

1. gRPC call fails with `ConnectionError`/`TimeoutError`/`OSError` → `_activate_fallback()` switches `self._engine` to `self._local_engine` (an in-process `PyEngine` wrapping a Rust `LocalEngine`).
2. During fallback, all memory writes (`write_memory`, `delete_memory`, `batch_write_memory`) go to the **local** `LocalEngine`'s `MemoryStore`, which uses `ShortTermMemory` backed by an **in-memory `Vec<(String, StoredEntry)>`** fallback (`crates/uc-engine/src/memory/short_term.rs:32`, `:117-124`), NOT TiKV.
3. `_check_grpc_recovery()` (`engine.py:235-265`) periodically probes gRPC `health()` + `list_tasks()`. On success, it flips back to gRPC mode — **but local fallback writes are silently abandoned**. There is no write-behind log, no replay buffer, no buffer at all.

### Key Data Types

- `MemoryEntry` (`crates/uc-types/src/memory.rs:38-45`): has `id: MemoryId` (UUID v4), `created_at`, `updated_at` (both `chrono::DateTime<Utc>`). **No version field, no logical clock, no cas_token.**
- `StoredEntry` (`short_term.rs:16-22`): the TiKV-serialized form. Stores `id`, `content`, `metadata`, `created_at`, `updated_at` as RFC3339 strings. **No version field.**
- `MemoryWriteRequest` (`memory.rs:85-89`): key + content + metadata. **No timestamp, no expected_version, no cas fields.**
- `WriteMemoryRequest` proto (`engine.proto:136-151`): **No version/timestamp fields.** Same for `BatchWriteMemoryRequest`.

### TiKV Client: Native CAS IS Available

The repo pins `tikv-client = "0.3.0"` (`Cargo.toml:38`). The `RawClient` in this version exposes:

- `with_atomic_for_cas(&self) -> Self` (`raw/client.rs:192`): returns a client clone in "atomic" mode. Required for CAS operations.
- `compare_and_swap(&self, key, previous_value: Option<Value>, new_value: Value) -> Result<(Option<Value>, bool)>` (`raw/client.rs:555-576`): native TiKV raw-KV CAS. Returns `(previous_value, swapped)`. `swapped=true` means the CAS succeeded.
- Regular `put`/`delete`/`get`/`scan` are all available (`raw/client.rs:283,349,221,440`).

**Important**: `compare_and_swap` requires `with_atomic_for_cas()` mode, which makes all writes on that client handle more expensive (the doc comment at `:185-190` warns: "write operations like put or delete in atomic mode are more expensive. Some operations are not supported in the mode").

### Current Write Path (No CAS)

`MemoryStore::write()` (`memory/mod.rs:123-172`): creates a fresh `MemoryEntry` with `MemoryId::new()` (new UUID) and `now` timestamps, then calls `self.short_term.write(&entry)`. `ShortTermMemory::write()` (`short_term.rs:165-198`) does a blind `client.put(encoded_key, value)` — **unconditional overwrite, no CAS**. The fallback in-memory path does find-and-replace in the Vec.

### Existing Conflict Infrastructure (Not Used for Memory)

- `ConflictDetector` (`crates/uc-engine/src/conflict.rs`): intent-based, for file edits (line ranges). Not applicable to memory KV.
- `DistributedConflictDetector` (Python): advisory scheduling hint, never touches network.
- `MergeArbiter`: git merge-time, for code branches. Not for memory.
- `CheckpointManager`: event sourcing for task state, not memory writes.

### NATS Broadcast (Already Exists for Cache Invalidation)

On normal gRPC memory writes, the worker broadcasts `uc.memory.changed` via NATS (`python/ultimate_coders/agent/worker.py:1199-1222`, `nats_worker.py:1728-1760`). Other workers clear their search cache. This is **not** replayed after fallback recovery currently.

---

## External Research Findings

### 1. Write-Behind / Replay-on-Reconnect Patterns

**Dynamo-style reconciliation (Amazon DynamoDB / Riak)**:
- Each write carries a vector clock (or causal context). On node reconnection, read-repair / active anti-entropy reconciles divergent replicas.
- Siblings (concurrent writes) are presented to the application for semantic resolution, or resolved by last-writer-wins (LWW) timestamp.
- For a single-gateway topology, full vector clocks are overkill; a simple (node_id, counter) or HLC suffices.

**CRDT (Conflict-free Replicated Data Types)**:
- Data structures that merge deterministically without coordination (e.g., G-Counter, LWW-Register, OR-Set).
- For memory KV where each key has a single writer at a time (one worker per subtask), a **LWW-Register CRDT** is the natural fit: each write carries a timestamp, merge = max timestamp wins. This is exactly the PRD's stated requirement.

**MongoDB retryable writes**:
- Each write gets a `txnNumber` + `stmtId`. On retry, the server recognizes the same txnNumber and returns the cached result (idempotent retry).
- This prevents duplicate application on retry, but does NOT solve cross-node conflict resolution. It's orthogonal — useful for ensuring replay doesn't double-apply, but not for LWW ordering.

**Redis AOF (Append-Only File) replay**:
- Redis replays the AOF on restart to reconstruct state. This is a local recovery mechanism, not a distributed conflict resolver. The pattern of "buffer writes locally, replay on connection restore" is directly analogous to our fallback buffer.

**Key takeaway**: The industry-standard pattern for this exact scenario (temporary disconnection + reconciliation) is a **write-ahead log (WAL) / write buffer** on the disconnected client, replayed on reconnect with **LWW by timestamp** conflict resolution. This is simpler than CRDTs or vector clocks for a hub-and-spoke topology.

### 2. Clock Options

**Wall-clock (NTP-synced)**:
- Simplest. `chrono::Utc::now()` (already used in the codebase).
- Risk: clock skew between workers. If worker A's clock is 2s ahead of worker B's, A's older write can clobber B's newer write.
- NTP typically keeps skew under 100ms on well-managed hosts. For task-scoped memory with human-paced writes, 100ms skew is acceptable.

**Hybrid Logical Clocks (HLC)**:
- Combines physical + logical components: `(physical_ts, logical_counter)`. Maintains causal ordering while staying close to wall-clock.
- Implementation: each node tracks a local HLC. On send/receive, HLC is updated per Lamport-style rules with physical-time fallback.
- Libraries: `hlc` crate (Rust), various Python implementations. Minimal overhead (~20 lines of logic).
- Eliminates the "clock skew clobbers newer write" problem. The logical component breaks ties when physical timestamps are within skew window.

**Vector clocks**:
- Track causal history per-node. Overkill for hub-and-spoke where there's one gateway. Memory size grows with node count. Not recommended.

**Recommendation for our topology**: Wall-clock is the **minimum viable** (the PRD already uses `chrono::Utc::now()`). HLC is the **safe upgrade** if clock skew proves problematic. Vector clocks are unnecessary. The single-gateway, multi-worker star topology means the gateway is the natural "causal anchor" — HLC on workers + gateway gives total order without mesh complexity.

### 3. TiKV Native CAS / Versioned Writes

Confirmed from source (`tikv-client 0.3.0`):

- **`RawClient::compare_and_swap(key, previous_value, new_value)`**: atomic CAS on raw KV. Requires `with_atomic_for_cas()` mode.
- Semantics: if the current value at `key` equals `previous_value`, replace with `new_value`. Returns `(old_value, swapped_bool)`. If `previous_value` is `None`, the CAS succeeds only if the key doesn't exist (insert-if-absent).
- **Limitation**: CAS compares by **exact byte equality** of the value. This means the "previous value" must be the exact bytes currently stored. For LWW, you'd encode a version/timestamp inside the value and do:
  1. `get(key)` → read current `StoredEntry`, extract its `version`/`updated_at`
  2. If your write's timestamp > stored timestamp: `compare_and_swap(key, current_bytes, new_bytes)`
  3. If CAS fails (someone else wrote), re-read and retry (or abandon if your write is stale)

- **No native versioned writes**: TiKV raw KV does not have MVCC-style versioned puts (that's the transactional API, which we don't use). Raw KV is plain key-value. Versioning must be **app-level** (embed version/timestamp in the serialized value).

- **Alternative: condition on timestamp prefix**: You could encode the key as `memory:{scope}:{id}:{version}` and use prefix scans + client-side merge. But this changes the key schema significantly and complicates reads.

### 4. Python→gRPC Gateway Offline-Write-Replay Patterns

**Temporal workers**:
- Temporal uses deterministic workflow replay, not write replay. Workers are stateless; the Temporal server holds all state. If a worker dies mid-activity, the activity is retried on another worker. There's no "offline buffer" because workers don't own state.
- Not directly applicable — our workers DO own local state during fallback.

**Celery workers with Redis**:
- Celery has `CELERY_TASK_ALWAYS_EAGER` and result backend fallback patterns, but no standard "offline write replay" mechanism. If Redis is down, tasks fail. Some users implement a local SQLite buffer + drain thread, but this is application-specific, not framework-level.

**gRPC client-side buffering (general pattern)**:
- Common in mobile/offline-first apps: buffer writes locally (SQLite/CoreData), sync on reconnect with server-assigned timestamps or vector clocks.
- The standard library for this pattern is **CRDT-based sync** (e.g., Automerge, Yjs) for collaborative editing, or simple **WAL + LWW** for KV stores.

**Key takeaway**: There's no off-the-shelf Python library for "gRPC write-behind with CAS replay." It's an application-level pattern: local WAL + timestamped entries + CAS-on-reconnect. This is what we must build.

---

## Concrete Approaches for Our Repo

### Approach A: Local WAL + App-Level LWW CAS (TiKV-native CAS)

**How it works**:
1. **Fallback write buffer**: In `Engine.__init__`, add `self._fallback_write_log: list[PendingWrite]` (a write-ahead log). Each entry: `(MemoryKey, MemoryContent, MemoryMetadata, timestamp, op_type: write|delete)`.
2. **During fallback**: `_try_grpc_with_fallback` detects fallback mode. For `write_memory`/`delete_memory`/`batch_write_memory`, after the local write succeeds, append the operation to `_fallback_write_log`. The local `LocalEngine` still writes to its in-memory fallback (unchanged).
3. **On recovery** (`_check_grpc_recovery` succeeds): drain `_fallback_write_log`. For each pending write:
   a. Call a new gRPC RPC `ReplayMemoryWrite(request)` that includes the `timestamp` and `expected_version` (the timestamp of the value currently in TiKV).
   b. Gateway-side: `MemoryStore::replay_write()` reads the current entry from TiKV, compares timestamps. If pending.timestamp > stored.timestamp: use `RawClient::with_atomic_for_cas().compare_and_swap(key, current_bytes, new_bytes)`. If CAS fails (concurrent write), re-read and retry once. If still failing, log conflict and keep the newer version.
   c. After successful replay, broadcast `uc.memory.changed` via NATS (existing path).
4. **Timestamp**: Use `chrono::Utc::now()` (wall-clock). Upgrade to HLC later if needed.

**Pros**:
- Uses TiKV's **native CAS** — no app-level locking on the gateway.
- Clean separation: fallback buffer is Python-side, CAS is Rust-side.
- Existing `MemoryEntry` gets a new `version: u64` field (or reuses `updated_at` timestamp as the version).
- Minimal proto changes: add `ReplayMemoryWrite` RPC + `expected_timestamp` field.

**Cons**:
- CAS requires `with_atomic_for_cas()` mode — more expensive writes. Mitigation: only use CAS mode for replay writes, not normal writes.
- CAS compares by exact bytes, so the gateway must read-then-compare-then-swap. There's a race between read and CAS (ABO/ABA problem). For LWW: re-read on CAS failure and compare timestamps again; if the stored timestamp is still newer, abandon the replay write (it's stale).
- Wall-clock skew risk. Workers must have NTP.

**Files it would touch**:
| File | Change |
|------|--------|
| `python/ultimate_coders/engine.py` | Add `_fallback_write_log`, drain logic in `_check_grpc_recovery`, wrap `write_memory`/`delete_memory`/`batch_write_memory` to log during fallback |
| `crates/uc-types/src/memory.rs` | Add `version: u64` (or `timestamp: i64`) to `MemoryEntry` / `StoredEntry` |
| `crates/uc-engine/src/memory/short_term.rs` | Add `write_cas()` method using `RawClient::with_atomic_for_cas().compare_and_swap()` |
| `crates/uc-engine/src/memory/mod.rs` | Add `replay_write()` to `MemoryStore` with timestamp comparison + CAS retry loop |
| `crates/uc-grpc/proto/engine.proto` | Add `ReplayMemoryWrite` RPC + message with `timestamp`/`version` field |
| `crates/uc-grpc/src/server.rs` | Handle `ReplayMemoryWrite` RPC |
| `crates/uc-grpc/src/client.rs` | Add `replay_memory_write()` client method |
| `crates/uc-python/src/engine.rs` | Expose `replay_memory_write` to Python via PyO3 |
| `python/ultimate_coders/agent/worker.py` | Call NATS broadcast after replay drain |

---

### Approach B: App-Level Versioning + Optimistic Concurrency (No TiKV CAS)

**How it works**:
1. Same fallback write buffer as Approach A (Python-side WAL).
2. On recovery, drain the buffer with a new `ReplayMemoryWrite` RPC that carries `timestamp`.
3. Gateway-side `MemoryStore::replay_write()`: read current entry, compare timestamps. If pending is newer: **blind `put`** (unconditional overwrite). No CAS.
4. To reduce the ABA race: use a **per-key lock** (in-memory `DashMap<MemoryKey, Mutex<()>>` on the gateway) during the read-compare-write sequence. Since there's a single gateway instance, this is safe within the process.

**Pros**:
- **No TiKV CAS mode needed** — all writes stay on the cheaper non-atomic `RawClient`. Better write throughput for normal path.
- Simpler Rust code (no `with_atomic_for_cas()`, no retry loop).
- Single-gateway = no cross-process lock needed. In-process mutex suffices.

**Cons**:
- **Not atomic across gateway restart**: if the gateway crashes between read and put, the replay is lost. (Mitigated by keeping the WAL until ACK.)
- The in-process lock adds contention on hot keys. For task-scoped memory (one writer per task), this is negligible.
- Slightly weaker guarantee than TiKV CAS — but in practice equivalent for single-gateway.

**Files it would touch**:
| File | Change |
|------|--------|
| `python/ultimate_coders/engine.py` | Same as Approach A |
| `crates/uc-types/src/memory.rs` | Add `version`/`timestamp` to `MemoryEntry` |
| `crates/uc-engine/src/memory/mod.rs` | Add `replay_write()` with in-process lock + timestamp compare + blind put |
| `crates/uc-engine/src/memory/short_term.rs` | No CAS method needed — use existing `write()` (blind put) |
| `crates/uc-grpc/proto/engine.proto` | Add `ReplayMemoryWrite` RPC |
| `crates/uc-grpc/src/server.rs` | Handle RPC |
| `crates/uc-grpc/src/client.rs` | Client method |
| `crates/uc-python/src/engine.rs` | PyO3 exposure |

---

### Approach C: HLC Timestamps + WAL + Gateway-Authoritative Ordering

**How it works**:
1. Each worker maintains an **HLC** (Hybrid Logical Clock). Every memory write stamps the entry with `(hlc_physical, hlc_logical)`.
2. Fallback write buffer same as A/B.
3. On recovery, drain with `ReplayMemoryWrite` carrying the HLC tuple.
4. Gateway receives the HLC, compares against its own HLC and the stored entry's HLC. Merge logic:
   - If `pending.hlc > stored.hlc`: write (blind put or CAS).
   - If `pending.hlc < stored.hlc`: skip (stale write).
   - If `pending.hlc == stored.hlc` (concurrent): tie-break by `worker_id` (deterministic, stable).
5. Gateway also updates its own HLC from the received HLC (standard HLC receive rule).

**Pros**:
- **Eliminates clock-skew false clobbering** — the logical component breaks ties within the skew window.
- Causally correct: if worker A's write causally preceded B's, A's HLC < B's HLC, so B wins even if A's wall clock was ahead.
- Future-proof for multi-gateway (HLC scales to N gateways without vector clocks).

**Cons**:
- More moving parts: HLC implementation in both Python (worker) and Rust (gateway).
- HLC must be persisted across worker restarts (otherwise logical counter resets). For a worker that crashes and restarts, the HLC can regress. Mitigation: persist HLC to a local file or use wall-clock as the physical floor (worst case degrades to LWW).
- Overkill for single-gateway if NTP is well-managed.

**Files it would touch**:
| File | Change |
|------|--------|
| All files from Approach A or B | Same |
| `crates/uc-types/src/memory.rs` | HLC tuple field `(phys: i64, logical: u64)` in `MemoryEntry` |
| `python/ultimate_coders/engine.py` or new `python/ultimate_coders/hlc.py` | HLC implementation (~30 lines) |
| `crates/uc-engine/src/hlc.rs` (new) | Rust HLC implementation (~30 lines) |
| `crates/uc-engine/src/memory/mod.rs` | HLC-aware `replay_write()` |

---

## Recommendation Summary

| Criterion | Approach A (TiKV CAS) | Approach B (App Lock) | Approach C (HLC) |
|-----------|----------------------|----------------------|-------------------|
| Atomicity guarantee | Strongest (TiKV-native) | Medium (in-process lock) | Depends on A or B base |
| Implementation effort | Medium-high | Low-medium | High |
| Clock skew resilience | Wall-clock dependent | Wall-clock dependent | Resilient (HLC) |
| Write path overhead | CAS mode is expensive | No overhead | No overhead (HLC is cheap) |
| Single-gateway fit | Good | Excellent | Good but over-engineered |

**For our constraints** (single gateway, TiKV raw KV, Python worker, Rust gateway):
- **Approach B is the pragmatic MVP**: minimal changes, in-process lock is safe for single-gateway, wall-clock LWW is acceptable with NTP. Ship this first.
- **Approach A is the "correct" upgrade** if we later see CAS failures or want TiKV-native atomicity. The `compare_and_swap` API is already available in our pinned `tikv-client 0.3.0`.
- **Approach C is the long-term solution** if clock skew causes real problems or if we ever go multi-gateway. HLC can be layered on top of either A or B.

The PRD's stated assumption ("worker与gateway时钟有合理同步 CAS时间戳用monotonic+wall混合或HLC") suggests the user is already leaning toward HLC (Approach C), but the MVP could start with B and upgrade.

---

## Caveats / Not Found

- **No external web search was performed** — the `mcp__exa__web_search_exa` and `mcp__exa__get_code_context_exa` tools listed in the system prompt were not available in this agent's function set. The external findings are synthesized from established distributed systems knowledge (Dynamo, CRDT, HLC literature) rather than live web sources. If cited URLs are needed, a follow-up research pass with actual web search is required.
- **TiKV transactional API not investigated**: The research focused on raw KV (what the codebase uses). TiKV's transactional KV API has native MVCC and `CheckNotExists`/compare-and-put primitives, but switching from raw to transactional mode would be a larger architectural change.
- **Qdrant CAS for long-term memory not investigated**: The research focused on short-term (TiKV) replay. Long-term (Qdrant) writes during fallback would need similar treatment but Qdrant's upsert semantics differ (point ID-based, no CAS). This is a separate research topic.
- **No benchmarking**: The "CAS mode is more expensive" claim is from tikv-client docs, not measured. Actual overhead on our workload is unknown.
- **Proto changes are breaking**: Adding fields to `MemoryEntryProto` and `WriteMemoryRequest` is backward-compatible (protobuf default semantics), but adding a new `ReplayMemoryWrite` RPC requires regenerating client stubs in both Rust and Python.
