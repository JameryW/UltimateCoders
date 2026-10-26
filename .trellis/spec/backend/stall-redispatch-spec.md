# Stall Redispatch Spec (Observe Surface)

> Progress-stop / `loop_detected` as a first-class structured signal (D7 / ticket 13).
> Enforce flag `UC_STALL_REDISPATCH_ENFORCE` defaults off; agent swap is ticket 14.

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/agent/stall_redispatch.py`,
`python/ultimate_coders/agent/worker.py` (failure_data on progress stop),
`python/ultimate_coders/agent/progress.py` (verdict source only — **no** replan API).

Trigger: any change to the stall redispatch flag, `StallSignal` / `build_stall_signal`,
or worker attachment of `stall_signal` beside `loop_detected`. Do **not** add
replan / agent-swap primitives to `progress.py`. Do **not** enable the flag by
default. Do **not** swap agents when the flag is on until ticket 14.

---

## 2. Signatures

```python
STALL_REDISPATCH_ENFORCE_ENV = "UC_STALL_REDISPATCH_ENFORCE"

@dataclass(frozen=True)
class StallSignal:
    loop_detected: bool
    stalled_streak: int
    reason: str
    error_signature: str
    subtask_id: str | None = None
    contract_digest: str | None = None

    def to_dict(self) -> dict: ...

def stall_redispatch_enforce_enabled() -> bool: ...

def build_stall_signal(
    verdict: ProgressVerdict,
    *,
    subtask_id: str | None = None,
    contract_digest: str | None = None,
) -> StallSignal | None: ...
```

`ProgressVerdict` is imported from `ultimate_coders.agent.progress`.

Truthy parser matches other UC flags: ``1`` / ``true`` / ``yes`` / ``on``
(case-insensitive). Unset / empty / other values → disabled.

---

## 3. Contracts

1. **Flag default off.** `UC_STALL_REDISPATCH_ENFORCE` is off unless truthy.
   T13 does not swap agents when the flag is on; ticket 14 owns one-shot
   same-contract redispatch.
2. **Signal only on stop.** `build_stall_signal` returns a `StallSignal` when
   `verdict.action == "stop"`, else `None`.
3. **Stable fields.** Required: `loop_detected` (always `True` on a built
   signal), `stalled_streak`, `reason`, `error_signature` (from
   `verdict.signature`). Optional identity: `subtask_id`, `contract_digest`
   when the caller has them.
4. **`to_dict`.** Emits the required fields; omits unset optional identity
   keys (no null placeholders).
5. **Worker seam.** On progress stop, keep `loop_detected=True` for compat and
   attach `failure_data["stall_signal"] = stall.to_dict()` so NATS /
   orchestrator consumers see structured data without log scraping.
6. **Flag off preserves stop-without-redispatch.** No alternate-agent swap
   from this module or from the worker on this ticket.
7. **No replan in progress.** `progress.py` remains stop/retry evidence only.

---

## 4. Validation & Error Matrix

| Condition | Required behaviour |
|---|---|
| Flag unset / falsey | `stall_redispatch_enforce_enabled()` → `False` |
| Flag truthy | enforce helper → `True`; still **no** agent swap in T13 |
| `verdict.action == "stop"` | `build_stall_signal` → `StallSignal` |
| `verdict.action == "retry"` | `build_stall_signal` → `None` |
| Worker progress stop | `loop_detected` + structured `stall_signal` on failure_data |
| Worker moving failures | no `loop_detected` / no `stall_signal` |

---

## 5. Good / Base / Bad Cases

- **Good:** stop verdict yields a dict with `loop_detected`, streak, reason,
  and error_signature; worker events carry `stall_signal`.
- **Base:** flag off; progress stop still stops retries without redispatch;
  `progress.py` unchanged as evidence-only.
- **Bad:** default-on flag; inventing replan inside `progress.py`; swapping
  agents in T13; requiring a live NATS cluster to unit-test the signal.

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
stall = build_stall_signal(verdict, subtask_id=subtask.id)
if stall is not None:
    failure_data["loop_detected"] = True
    failure_data["stall_signal"] = stall.to_dict()
# Ticket 14 may later read stall_redispatch_enforce_enabled() to redispatch once.
```
