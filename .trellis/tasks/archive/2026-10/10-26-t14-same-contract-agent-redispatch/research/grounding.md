# T14 grounding — same-contract one-shot agent redispatch

Authority: ticket 14, D7, spec-p2 Seam G, T13 observe surface already in tree.

## 1. Where `stall_signal` / `loop_detected` appears today

| Path | Role |
|---|---|
| `python/ultimate_coders/agent/stall_redispatch.py` | `StallSignal`, `build_stall_signal`, `stall_redispatch_enforce_enabled` (flag default off). **No swap yet.** |
| `python/ultimate_coders/agent/worker.py` ~1229–1316 | On progress `verdict.action == "stop"`: sets `failure_data["loop_detected"]=True`, attaches `failure_data["stall_signal"]=stall.to_dict()`, then forces `result.retryable=False` and publishes terminal `subtask_failed`. |
| `python/ultimate_coders/agent/progress.py` | Evidence-only stop/retry; **must stay free of replan/swap**. |
| `tests/python/test_stall_signal_surface.py` | T13 contracts. |
| `.trellis/spec/backend/stall-redispatch-spec.md` | Observe-half spec; explicitly defers swap to T14. |

Gap: worker calls `build_stall_signal(verdict, subtask_id=...)` but does **not** pass `contract_digest` today (field exists, optional).

## 2. Retry vs terminal after `subtask_failed`

Three layers, do not conflate:

1. **Worker inner loop** (`Worker.MAX_RETRIES` / `RETRY_DELAYS`): retries only when `verdict.action == "retry"` and classifiers allow. Stall stop → `retryable=False` (stops both inner loop and intending to stop outer replay).
2. **NATS JetStream redelivery**: max_deliver poison → terminal `subtask_failed`; capability miss → nak. Stall already marks non-retryable in the outcome payload (`retryable` on result event ~3261).
3. **Orchestrator / gateway**:
   - Python `Orchestrator.handle_subtask_result` marks `COMPLETED`/`FAILED` from `result.success` and **does not** auto-reopen on `retryable`.
   - Gateway `TaskStore.retry_subtask` (`crates/uc-grpc/src/server.rs`) is the **explicit** Failed→Pending + `dispatch_retry_count+=1` CAS (operator/API path). No automatic stall→retry today.
   - Dispatch pickup caps at `dispatch_retry_count >= 3` when Pending with no capable worker.

Implication for T14: one-shot same-contract swap is **new** policy. It should reuse Failed→Pending / attempt bump (or an equivalent local continue) without inventing a second retry subsystem. Flag off must keep today’s terminal stall stop.

## 3. How coding agents are selected

| Mechanism | Path |
|---|---|
| Default env | `UC_CODING_AGENT` → `SandboxConfig.agent` (default `grok-build`) in `nats_worker.py` ~1563–1569 |
| Plugin registry | `python/ultimate_coders/agent/registry.py` — `AgentPluginSpec`, `register_agent`, `available_agents()`, `create_adapter`, `capability_names` (discoverable + CLI probe) |
| Sandbox facade | `sandbox.available_agents()` / `create_adapter()` delegate to registry |
| Per-subtask override | `Subtask.agent_config` (keys include `agent` / `agent_name`, tools, prompts); `Worker._resolve_agent_config` priority: explicit `agent_config` > capability profiles > description templates |
| Execute seam | `_execute_in_sandbox` passes `agent=agent_config.get("agent")` into sandbox |

Alternate agent for T14 should come from **discoverable registered names excluding current canonical agent**, not a hard-coded list and not inventing a parallel registry.

## 4. Contract digest identity fields

| Source | Fields |
|---|---|
| `python/ultimate_coders/agent/contract.py` | `contract_digest(text)`, `ContractRegistry.observe` → `ContractVerdict` (`first_seen` / `unchanged` / `changed` + version). Digest = SHA1 of cosmetics-normalized goal text. |
| Dispatch contract text | `Subtask.user_request` (task-level goal). Empty → no contract gate (legacy). |
| NATS gate | `nats_worker` ~3087–3128: on `changed` → refuse with `contract_changed` + versions; **no execute**. |
| StallSignal | Optional `contract_digest` already on dataclass / `to_dict`. |
| Envelope (orthogonal) | `CONTRACT_VERSION` / `ExecutionEnvelope` — worker↔gateway protocol lockstep, **not** goal digest. Do not conflate. |

Same-contract redispatch must keep the same `user_request` / digest (registry `unchanged`) and must not look like `contract_changed`. Do not bump contract version or rewrite goal text (true replan = T18).

## 5. Minimal vertical slice (recommended)

Prefer **pure helpers in `stall_redispatch.py`** (unit-testable without NATS), then a thin wire:

```python
# Recommended additions (names illustrative; keep frozen dataclasses)

@dataclass(frozen=True)
class StallEpisodeState:
    swaps_used: int = 0          # quota: 0 or 1 per stall episode
    last_agent: str | None = None
    contract_digest: str | None = None

@dataclass(frozen=True)
class RedispatchDecision:
    action: str  # "noop" | "swap_once" | "escalate"
    alternate_agent: str | None = None
    reason: str = ""

def select_alternate_agent(current: str, candidates: Sequence[str]) -> str | None: ...
def decide_stall_redispatch(
    *,
    enforce: bool,
    stall: StallSignal | None,
    episode: StallEpisodeState,
    current_agent: str,
    available: Sequence[str],
    expected_digest: str | None = None,
) -> RedispatchDecision: ...
```

Behaviour matrix:

| Condition | Decision |
|---|---|
| Flag off / no stall | `noop` (today’s terminal stop) |
| Flag on + stall + `swaps_used==0` + alternate exists + digest matches (or unset) | `swap_once` |
| Flag on + stall + already swapped / no alternate | `escalate` (non-retryable terminal / human) |

**Wire (thin):** after stall attachment in worker and/or on orchestrator ingest of `subtask_failed` carrying `stall_signal` — read flag + episode + decide; on `swap_once` stamp `agent_config["agent"]=alternate`, reopen attempt under **same** `user_request` (digest unchanged); on second stall escalate. Prefer orchestrator/dispatch-owner ownership per D7/spec-p2; worker may attach suggested alternate + digest for observability.

Pass `contract_digest=contract_digest(subtask.user_request)` into `build_stall_signal` when `user_request` is non-empty (T13 gap fill; still observe-compatible).

## What NOT to invent

- Replan / swap API inside `progress.py`
- New feature-flag name (reuse `UC_STALL_REDISPATCH_ENFORCE`)
- Multi-agent cascade / more than one automatic swap per stall episode
- Goal rewrite or contract version bump (T18)
- Parallel agent registry or hard-coded alternate list
- Conflating `CONTRACT_VERSION` envelope with goal `contract_digest`
- Requiring live NATS for unit tests of decision helpers
- NetworkMode / Landlock (T15+)
- Treating `retryable=True` alone as the swap trigger (stall path already forces False)
