# Stall Redispatch Spec (Observe + Enforce)

> Progress-stop / `loop_detected` as a first-class structured signal (D7 / T13)
> plus same-contract one-shot alternate-agent redispatch (T14).
> Enforce flag `UC_STALL_REDISPATCH_ENFORCE` defaults off. True replan
> (new decomposition) is deferred to T18.

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/agent/stall_redispatch.py`,
`python/ultimate_coders/agent/worker.py` (failure_data + `result.stall_signal`),
`python/ultimate_coders/agent/orchestrator.py` (Failed→Pending wire),
`python/ultimate_coders/agent/progress.py` (verdict source only — **no** replan API).

Trigger: any change to the stall redispatch flag, `StallSignal` /
`build_stall_signal`, episode / decision helpers, or orchestrator apply of
`swap_once`. Do **not** add replan / agent-swap primitives to `progress.py`.
Do **not** enable the flag by default. Do **not** bump contract version or
rewrite goal text on redispatch.

---

## 2. Signatures

```python
STALL_REDISPATCH_ENFORCE_ENV = "UC_STALL_REDISPATCH_ENFORCE"
STALL_EPISODE_KEY = "_uc_stall_episode"

@dataclass(frozen=True)
class StallSignal:
    loop_detected: bool
    stalled_streak: int
    reason: str
    error_signature: str
    subtask_id: str | None = None
    contract_digest: str | None = None

    def to_dict(self) -> dict: ...

@dataclass(frozen=True)
class StallEpisodeState:
    swaps_used: int = 0
    last_agent: str | None = None
    contract_digest: str | None = None

@dataclass(frozen=True)
class RedispatchDecision:
    action: str  # "noop" | "swap_once" | "escalate"
    alternate_agent: str | None = None
    reason: str = ""

def stall_redispatch_enforce_enabled() -> bool: ...

def build_stall_signal(
    verdict: ProgressVerdict,
    *,
    subtask_id: str | None = None,
    contract_digest: str | None = None,
) -> StallSignal | None: ...

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

def apply_stall_redispatch_decision(
    subtask,
    decision: RedispatchDecision,
    episode: StallEpisodeState,
) -> StallEpisodeState: ...

def maybe_apply_stall_redispatch(
    subtask,
    *,
    stall: StallSignal | None,
    available: Sequence[str],
    enforce: bool | None = None,
    current_agent: str | None = None,
) -> RedispatchDecision: ...
```

`ProgressVerdict` is imported from `ultimate_coders.agent.progress`.

Truthy parser matches other UC flags: ``1`` / ``true`` / ``yes`` / ``on``
(case-insensitive). Unset / empty / other values → disabled.

---

## 3. Contracts

### Observe (T13)

1. **Flag default off.** `UC_STALL_REDISPATCH_ENFORCE` is off unless truthy.
2. **Signal only on stop.** `build_stall_signal` returns a `StallSignal` when
   `verdict.action == "stop"`, else `None`.
3. **Stable fields.** Required: `loop_detected` (always `True` on a built
   signal), `stalled_streak`, `reason`, `error_signature` (from
   `verdict.signature`). Optional identity: `subtask_id`, `contract_digest`
   when the caller has them.
4. **`to_dict`.** Emits the required fields; omits unset optional identity
   keys (no null placeholders).
5. **Worker seam.** On progress stop, keep `loop_detected=True`, attach
   `failure_data["stall_signal"]` and `result.stall_signal`, and pass
   `contract_digest=contract_digest(user_request)` when `user_request` is
   non-empty.
6. **No replan in progress.** `progress.py` remains stop/retry evidence only.

### Enforce (T14)

7. **Flag off → noop.** `decide_stall_redispatch` returns `noop`; orchestrator
   marks Failed as today (stop-without-redispatch).
8. **One swap.** Flag on + stall + `swaps_used==0` + alternate registered
   agent + digest compatible → `swap_once`. Quotas: exactly one automatic
   swap per stall episode.
9. **Escalate.** Second stall (`swaps_used>=1`), no alternate, or digest
   mismatch → `escalate` (non-retryable terminal / human). No further
   automatic swaps.
10. **Same digest.** Redispatch must not bump contract version or rewrite
    `user_request` / goal text (that path is true replan / T18).
11. **Wire owner.** Orchestrator / dispatch applies Failed→Pending +
    `agent_config["agent"]=alternate` via `apply_stall_redispatch_decision`
    / `maybe_apply_stall_redispatch`. Worker does **not** inline-swap.
12. **Alternate selection.** `select_alternate_agent` picks the first
    discoverable registered name ≠ current (stable registry list order).

---

## 4. Validation & Error Matrix

| Condition | Required behaviour |
|---|---|
| Flag unset / falsey | `stall_redispatch_enforce_enabled()` → `False`; decide → `noop` |
| Flag truthy | enforce helper → `True` |
| `verdict.action == "stop"` | `build_stall_signal` → `StallSignal` |
| `verdict.action == "retry"` | `build_stall_signal` → `None` |
| Worker progress stop | `loop_detected` + structured `stall_signal` on failure_data / result |
| Worker moving failures | no `loop_detected` / no `stall_signal` |
| Flag off + stall at orchestrator | terminal Failed; no agent swap |
| Flag on + first stall + alternate + same digest | Pending + stamped alternate; `swaps_used=1` |
| Flag on + second stall / no alternate | escalate; stay Failed; `retryable=False` |

---

## 5. Good / Base / Bad Cases

- **Good:** stop verdict yields a dict with `loop_detected`, streak, reason,
  and error_signature; worker events carry `stall_signal`; flag on yields
  exactly one alternate-agent redispatch under unchanged digest.
- **Base:** flag off; progress stop still stops retries without redispatch;
  `progress.py` unchanged as evidence-only.
- **Bad:** default-on flag; inventing replan inside `progress.py`; worker-inline
  agent swap; multi-swap cascades; goal rewrite / contract version bump;
  requiring a live NATS cluster to unit-test helpers.

---

## 6. Tests Required

| Contract | Test |
|---|---|
| Flag default off / truthy / falsey | `tests/python/test_stall_signal_surface.py` |
| `build_stall_signal` on stop vs retry | same file |
| `StallSignal.to_dict` shape | same file |
| Worker attaches `stall_signal` on stop | same file |
| `progress.py` has no replan / swap API | same file |
| Existing progress detector | `tests/python/test_progress_detector.py` stays green |
| decide / select / episode helpers | `tests/python/test_stall_agent_redispatch.py` |
| Flag-off noop / swap-once / escalate wire | same file |
| Digest unchanged on swap | same file |

---

## 7. Wrong vs Correct

#### Wrong

```python
# Replan / swap inside the progress detector
class ProgressTracker:
    def observe(...):
        if stalled:
            return ProgressVerdict("replan", ...)  # forbidden
```

#### Correct

```python
verdict = progress_tracker.observe(evidence)
stall = build_stall_signal(
    verdict,
    subtask_id=subtask.id,
    contract_digest=contract_digest(subtask.user_request) if subtask.user_request else None,
)
if stall is not None:
    failure_data["loop_detected"] = True
    failure_data["stall_signal"] = stall.to_dict()
    result.stall_signal = stall.to_dict()

# Orchestrator (T14) — pure helpers + Failed→Pending wire:
decision = maybe_apply_stall_redispatch(
    subtask, stall=stall, available=available_agents()
)
if decision.action == "swap_once":
    # subtask already Pending with agent stamped; do not mark Failed
    ...
```
