# V9 Architecture Upgrade Map

This map records the six opt-in upgrades in `python/ultimate_coders/architecture/`.
Every component is independently flag-gated, defaults to off, and is safe to
import when its flag is unset. The package does not create a second scheduler,
a second knowledge authority, or a second mandate store.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Adaptive retry routing | `behavior_router`, `mandate`, `sync_async_promotion` | `UC_ADAPTIVE_ADAPTER_ROUTING`, `UC_MANDATE_ENVELOPE`, `UC_SYNC_ASYNC_PROMOTION` | worker retry path | modules tested; adapter routing wired; mandate and promotion are opt-in primitives |
| Retrieval arbitration | `memcredit` | `UC_MEMCREDIT` | worker search context | wired |
| Bitemporal knowledge | `bitemporal` | `UC_BITEMPORAL_KNOWLEDGE` | knowledge read face and recovery view | projection wired |
| Attempt evolution | `evolution` | `UC_AST_EVOLUTION` | recovery context | wired |
| Worker context folding | `context_folding` | `UC_ACP_FOLDING` | local harness loop | wired |
| Process acceptance | `process_verifier` | `UC_PROCESS_VERIFIER_ENFORCE` | worker success path | wired |
| Sandbox credentials | `credential_broker` | `UC_CREDENTIAL_BROKER` | sandbox environment contract | primitive + tests |
| Trajectory export | `trajectory_export` | `UC_TRAJECTORY_EXPORT` + `UC_TRAJECTORY_EXPORT_DIR` | caller-owned exporter | primitive + tests |

## Scheduling and retry

`behavior_router` keeps an in-process `AdapterBehaviorProfile` table, a local
TF-IDF task-similarity index, and a complementary-adapter scorer. The PostgreSQL
DDL is exported by `postgres_ddl()`; the in-memory store is deliberately the
default so a missing database never disables the retry path. The worker loads
profile rows from `UC_ADAPTER_PROFILES_JSON`, excludes the adapter that just
failed, and writes the chosen adapter into `subtask.agent_config` before the next
attempt. The decision is emitted as `adapter` on the failure event; prompts and
profile text never enter the event stream.

`mandate` is an immutable narrowing envelope over the existing governance
authority. It validates expiry and scope and can only narrow; it does not mint
authority and is not a replacement for the Rust governance store.

`sync_async_promotion` records a pending decision, promotes it after the sync
budget, and emits bounded convergence notices. It is a clock-injected primitive
for the Event Gate boundary: the orchestrator owns when to call `submit` and
when to publish `notices()`.

## Retrieval and context

`memcredit` selects repository candidates by marginal utility per token. It
uses the tight attempt budget (`800`) for a first attempt and the wider retry
budget (`3000`), suppresses token-Jaccard overlap, and gives same-directory
subtree hits a `1.5x` weight through `DirectoryTrie`. The worker passes the
already-ranked hybrid-search hits, so the permission and index boundaries stay
where they were; MemCredit only arbitrates the final prompt slice.

`context_folding` implements bounded ACP interval folding. At the configured
threshold it keeps the initial instruction pair and a safe tail, replaces the
middle with a deterministic summary, and records verified read hashes in
checkpoints. The local harness also blocks an unchanged repeated `read_file`
range. Checkpoint metadata is available through `available_checkpoints_json` so
a future UI can inspect folds without reading prompts.

## Knowledge

`bitemporal` is a projection layer, not a new store. Claims carry a
`valid_from_commit`, optional `expired_at_commit`, and an `EpistemicStatus`
(`unverified_claim`, `oracle_verified`, `negative_pitfall`). Expiry is soft and
records are never deleted; `as_of` answers point-in-time reads and `resolve`
collapses duplicate statements by status, confidence, and recency. The
knowledge read face projects its already-permission-filtered claims through the
graph when the flag is on, preserving claim ids.

`evolution` compacts `AttemptRound` patches into AST-anchored
added/removed/modified changes, detects reversions, clips values, and renders a
compact `~ old -> new` chain with `← reverted vN` markers. The recovery path
adds the rendered chain as an EVIDENCE item and records only counts in the
recovery summary.

## Worker context and acceptance

The local harness creates an `ACPFolder` and `ReadLedger` only when folding is
enabled. Folding runs between tool batches; duplicate reads are rejected with a
typed tool result rather than silently repeated.

`process_verifier` compares a changed test file at `HEAD` with its post-run
content and reports deleted assertions, test bypasses, swallowed exceptions, and
lucky passes. When enforcement is enabled, the worker converts the report's
errors into a failed subtask result before publishing success. The check is
deterministic and does not call an LLM.

## Sandbox and trajectory boundary

`credential_broker` issues short-lived HMAC-signed capability leases and exposes
a `SandboxNetworkPolicy` that defaults to no egress and no connect-back. The
policy's `to_env()` form is the contract a sandbox launcher consumes; host-side
secret injection must stay at the broker, never in the child environment.

`trajectory_export` writes redacted TITO JSONL records
(`token_id`, `logprob`, `loss_mask`) with bounded rotation, and pairs failed and
rescued attempts. Token ids and logprobs are only available from the inference
surface that produced them, so a caller that lacks them must omit the fields
rather than invent them.

## Rollout and rollback

All flags are read per call, so a restart with the flag unset restores the
previous behavior. The in-memory stores are bounded and process-local. No
database migration is required; the PostgreSQL DDL for adapter profiles is an
optional mirror, not a startup dependency.

Verification:

```bash
.venv/Scripts/python.exe -m pytest tests/python/test_architecture_*.py -q
.venv/Scripts/python.exe -m ruff check python/ultimate_coders/architecture
```

## Non-goals

- No kernel-level network isolation is claimed by `SandboxNetworkPolicy`; it is
  an explicit environment contract for the sandbox launcher.
- No new knowledge database, entity merge, or authority service.
- No timer thread or event bus in the promotion primitive.
- No synthetic token ids or logprobs in trajectory records.
