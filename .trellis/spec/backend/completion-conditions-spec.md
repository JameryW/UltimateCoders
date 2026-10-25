# Completion Conditions Spec

> Structured, non-LLM completion evaluation beside contract digest (D3 / tickets 05–06).

---

## 1. Scope / Trigger

Two namespaces share the product home `ultimate_coders.agent.contract`:

| Namespace | Responsibility | Module |
|---|---|---|
| **Digest / registry** | Goal-change detection only (`contract_changed`) | `contract.py` (`ContractRegistry`, `contract_digest`) |
| **Completion conditions** | Machine-checkable “done” | `completion_conditions.py` (re-exported from `contract`) |

Trigger: any change to completion condition types, evaluate/gate APIs, or the
`UC_COMPLETION_CONDITIONS_ENFORCE` flag. Do **not** fold completion into digest
records or give the model a self-report path that alone satisfies completion.
Do **not** introduce a second completion registry — adapters are condition types
under the same `evaluate_completion_conditions` authority.

---

## 2. Signatures

```python
COMPLETION_CONDITIONS_ENFORCE_ENV = "UC_COMPLETION_CONDITIONS_ENFORCE"

@dataclass(frozen=True)
class CompletionCondition:
    type: str
    params: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ConditionResult:
    type: str
    passed: bool
    reason: str

@dataclass(frozen=True)
class CompletionVerdict:
    satisfied: bool
    results: list[ConditionResult]

def completion_conditions_enforce_enabled() -> bool: ...

def evaluate_completion_conditions(
    conditions: Sequence[CompletionCondition | Mapping[str, Any]],
    evidence: Mapping[str, Any],
) -> CompletionVerdict: ...

def completion_gate(
    conditions: Sequence[CompletionCondition | Mapping[str, Any]],
    evidence: Mapping[str, Any],
) -> CompletionVerdict: ...
```

Imports may use either:

- `from ultimate_coders.agent.contract import evaluate_completion_conditions, ...`
- `from ultimate_coders.agent.completion_conditions import ...`

---

## 3. Contracts

1. **Empty list → satisfied.** No conditions means nothing to check.
2. **Non-empty → all must pass.** Aggregate `satisfied` is the conjunction of
   per-condition `passed` flags.
3. **Unknown type → fail closed.**
4. **Self-report types never pass alone.** Types `model_approved`, `llm_done`,
   `approved`, and `done` always fail, even when evidence contains matching
   truthy keys.
5. **Built-in `evidence_flag`.** `params={"name": "x"}` passes iff
   `evidence.get("x") is True` (strict identity, not truthiness).
6. **`verify_command` adapter.** Passes iff `evidence.get("verify_command_passed")
   is True`. The caller runs the command elsewhere and supplies the boolean;
   optional `params["cmd"]` is diagnostics-only and must not itself execute.
7. **`step_condition` adapter.** `params={"expr": "<grammar>"}` evaluates via
   `ultimate_coders.agent.step_condition.evaluate` against `evidence["prev"]`
   (an `AgentOutput` or `None`). Parse/eval errors → fail closed. Empty expr
   string follows step_condition semantics (always true).
8. **`independent_reeval` condition.** Passes iff evidence carries a passing
   sealed re-eval: prefer `evidence["independent_reeval"]` as
   `IndependentReevalOutcome` with `.passed is True`, or
   `evidence.get("independent_reeval_passed") is True`. Model or review
   self-approval keys alone must not satisfy this type.
9. **`review_approved` adapter.** Passes only when
   `evidence.get("review_verified") is True`. Review-node JSON approval alone
   never bypasses non-LLM evaluate.
10. **Evaluate is pure.** `evaluate_completion_conditions` does not read the
    enforce flag and does not touch `ContractRegistry` / digests. It does not
    shell out for `verify_command`.
11. **Flag default off.** `UC_COMPLETION_CONDITIONS_ENFORCE` is off unless set to
    a truthy value (`1` / `true` / `yes` / `on`). Product callers that must not
    gate when the flag is off use `completion_gate` (returns
    `satisfied=True` when off; otherwise delegates to evaluate).
12. **Digest unchanged.** Completing or failing conditions must not bump or
    rewrite contract digest versions.
13. **One authority.** Adapters are types in this module — no parallel “done”
    registry.

---

## 4. Validation & Error Matrix

| Input | Behavior |
|---|---|
| `conditions=[]` | `satisfied=True`, empty `results` |
| Unknown `type` | that result `passed=False`; aggregate unsatisfied |
| Self-report type | `passed=False` with self-report reason |
| `evidence_flag` missing / non-`True` name | fail closed |
| `verify_command` without `verify_command_passed is True` | fail closed |
| `step_condition` missing `expr` string | fail closed |
| `step_condition` parse/eval error | fail closed (no raise to caller) |
| `independent_reeval` without outcome/flag pass | fail closed |
| `review_approved` without `review_verified is True` | fail closed |
| Enforce flag off + `completion_gate` | `satisfied=True` without evaluating |
| Enforce flag on + `completion_gate` | same as evaluate |

---

## 5. Good / Base / Bad Cases

- **Good:** `evidence_flag` + `verify_command` + `independent_reeval` all True → satisfied.
- **Base:** empty conditions → satisfied; flag off → gate satisfied.
- **Bad:** `approved`/`done` alone; `independent_reeval` with only review self-approval;
  `review_approved` with only review JSON; unknown type; step expr parse error.

---

## 6. Tests Required

Prior art: `tests/python/test_completion_conditions.py` (seam),
`tests/python/test_completion_adapters.py` (adapters / re-eval), and
`tests/python/test_contract_versioning.py` (digest/registry must stay green).

Assert: flag default off; evaluate always computes; adapters under one evaluate;
self-approval cannot satisfy `independent_reeval`; digest APIs untouched.

---

## 7. Wrong vs Correct

| Wrong | Correct |
|---|---|
| Shell out inside `verify_command` evaluate | Caller supplies `verify_command_passed` boolean |
| Trust review JSON `approved` as completion | Require `review_verified is True` or fail |
| Second “done” registry beside this module | New types only in `evaluate_completion_conditions` |
| Gate on evaluate when flag off | Use `completion_gate` for product opt-in |
