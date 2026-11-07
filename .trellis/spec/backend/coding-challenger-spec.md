# Coding Challenger Spec (Champion / Challenger observe)

> Same-contract one-shot report-role challenger after a coding subtask
> **succeeds** (V8 P2 / D8). Flag `UC_CODING_CHALLENGER_ENFORCE` defaults off.
> MVP is compare/observe only — **no** auto-promote of challenger over
> champion. Stall-path swaps remain `UC_STALL_REDISPATCH_ENFORCE` (T14).

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/agent/coding_challenger.py`,
`python/ultimate_coders/agent/orchestrator.py` (success→Pending wire +
observe-only finalize).

Trigger: any change to the coding challenger flag, episode / decision
helpers, `eval_role=report` stamp, or orchestrator apply of
`challenge_once`. Do **not** enable the flag by default. Do **not** bump
contract version or rewrite goal text on challenge. Do **not** auto-promote
challenger artifacts over the champion.

---

## 2. Signatures

```python
CODING_CHALLENGER_ENFORCE_ENV = "UC_CODING_CHALLENGER_ENFORCE"
CODING_CHALLENGER_AGENT_ENV = "UC_CODING_CHALLENGER_AGENT"
CHALLENGER_EPISODE_KEY = "_uc_challenger_episode"
CHALLENGER_META_KEY = "_uc_challenger"

@dataclass(frozen=True)
class ChallengerEpisodeState:
    swaps_used: int = 0
    last_agent: str | None = None
    contract_digest: str | None = None

@dataclass(frozen=True)
class ChallengerDecision:
    action: str  # "noop" | "challenge_once"
    challenger_agent: str | None = None
    reason: str = ""

def coding_challenger_enforce_enabled() -> bool: ...
def preferred_challenger_agent() -> str | None: ...
def decide_coding_challenger(...) -> ChallengerDecision: ...
def apply_coding_challenger_decision(...) -> ChallengerEpisodeState: ...
def maybe_apply_coding_challenger(...) -> ChallengerDecision: ...
def is_report_challenger_episode(agent_config) -> bool: ...
def finalize_coding_challenger_observation(subtask, result) -> SubtaskResult: ...
```

Truthy parser matches other UC flags: ``1`` / ``true`` / ``yes`` / ``on``
(case-insensitive). Unset / empty / other values → disabled.

---

## 3. Contracts

1. **Flag default off.** `UC_CODING_CHALLENGER_ENFORCE` is off unless truthy.
2. **Flag off / not success → noop.** Orchestrator marks Completed as today.
3. **One challenge.** Flag on + first success + alternate registered agent +
   digest compatible → `challenge_once`. Exactly one challenger per episode.
4. **Quota / no alternate → noop.** Keep champion Completed; no reopen.
5. **Same digest.** Challenge must not bump contract version or rewrite
   `user_request` / goal text (true replan / T18 is out of scope).
6. **Report role.** `apply` stamps `agent_config["agent"]=challenger` and
   `eval_role=report`, bumps `dispatch_retry_count`, stashes champion compare
   fields under `_uc_challenger`.
7. **Wire owner.** Orchestrator applies success→Pending via
   `maybe_apply_coding_challenger`. Worker does **not** inline-challenge.
8. **Observe-only terminal.** When `_uc_challenger_episode.swaps_used >= 1`
   and `eval_role=report`, the next result **Completes** the subtask:
   - Challenger success → Completed with challenger result; meta records
     `challenger_success=True` (still no auto-promote of work products).
   - Challenger failure → Completed with champion-shaped successful result
     (`champion_summary`); meta records challenger failure. **Do not** mark
     Failed and **do not** run stall redispatch on the report attempt.
9. **Failure/stall outside challenger.** Coding challenger policy does not
   run on champion failure/stall; T14 owns that path.
10. **Alternate selection.** Prefer `UC_CODING_CHALLENGER_AGENT` when valid
    and ≠ current; else first registry alternate via `select_alternate_agent`.

---

## 4. Validation & Error Matrix

| Condition | Required behaviour |
|---|---|
| Flag unset / falsey | enforce helper → `False`; decide → `noop` |
| Flag on + first success + alternate + same digest | Pending + `eval_role=report`; `swaps_used=1` |
| Flag on + second success (quota used) | Completed; meta has challenger outcome |
| Flag on + challenger failure | Completed with champion authority; no stall swap |
| Flag on + no alternate | Completed (champion); no reopen |
| Digest mismatch | `noop` |
| Champion failure/stall | no coding challenger; T14 may apply |

---

## 5. Tests Required

| Contract | Test |
|---|---|
| Flag default off / decide noop | `tests/python/test_coding_challenger.py` |
| challenge_once + eval_role + digest preserve | same file |
| Quota / no alternate / digest mismatch | same file |
| Orchestrator flag-off / challenge / complete | same file |
| Orchestrator challenger failure → champion Completed | same file |
| Preferred env agent | same file |
| Stall suite stays green | `test_stall_agent_redispatch.py` / `test_stall_signal_surface.py` |

---

## 6. Wrong vs Correct

#### Wrong

```python
# Demoting champion because the report-role challenger failed
if not result.success:
    st.status = SubtaskStatus.FAILED  # forbidden for eval_role=report episode
```

#### Correct

```python
if is_report_challenger_episode(st.agent_config):
    result = finalize_coding_challenger_observation(st, result)
    st.status = SubtaskStatus.COMPLETED
    st.result = result
```
