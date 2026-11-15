"""Evidence-first knowledge layer for UltimateCoders.

The package separates four concerns the V2.0 upgrade must not conflate:

* :mod:`ultimate_coders.knowledge.model`   - shared vocabulary (visibility,
  scope, revision binding, evidence kinds, digests).
* :mod:`ultimate_coders.knowledge.rollout` - fail-closed rollout stages and
  budgets read from ``UC_*`` environment variables.
* ``attempt_chain`` - immutable, idempotent record of what each attempt
  actually changed (patch rounds) with AST-first symbol location.
* ``evidence`` / ``gate`` - evidence/claim/validation ledger plus the
  epistemic gate that decides whether knowledge may be shared or promoted.
* ``recovery`` - bounded retry recovery view compiled from prior attempt
  evidence and injected as ordinary ``ContextItem`` evidence.

Submodules other than ``model``/``rollout`` import ``runtime_state`` and are
therefore worker-side only; nothing here may be imported by the Rust facade.
"""
