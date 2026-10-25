# Completion Conditions Spec

> Structured, non-LLM completion evaluation beside contract digest (D3 / ticket 05).

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

Out of scope here (ticket 06): `verify_command` / `step_condition` adapters and
the `independent_reeval` condition type.

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
6. **Evaluate is pure.** `evaluate_completion_conditions` does not read the
   enforce flag and does not touch `ContractRegistry` / digests.
7. **Flag default off.** `UC_COMPLETION_CONDITIONS_ENFORCE` is off unless set to
   a truthy value (`1` / `true` / `yes` / `on`). Product callers that must not
   gate when the flag is off use `completion_gate` (returns
   `satisfied=True` when off; otherwise delegates to evaluate).
8. **Digest unchanged.** Completing or failing conditions must not bump or
   rewrite contract digest versions.

---

## 4. Tests

Prior art: `tests/python/test_completion_conditions.py` (seam) and
`tests/python/test_contract_versioning.py` (digest/registry must stay green).
