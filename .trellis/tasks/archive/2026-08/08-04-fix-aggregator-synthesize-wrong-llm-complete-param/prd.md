# Fix aggregator _synthesize wrong LLM complete param

## Goal

Fix the bug flagged in #554's trellis-implement: `ResultAggregator._synthesize` calls `self._llm_client.complete(prompt=prompt, max_tokens=1024)`, but `LLMClient.complete`'s signature is `complete(messages: list[dict], system=, max_tokens=, **kwargs)`. The `prompt=` kwarg falls into `**kwargs` and is silently ignored → the LLM runs with an empty messages list → synthesis is broken/empty.

## What I already know

* `aggregator.py:250`: `result = await self._llm_client.complete(prompt=prompt, max_tokens=1024)`.
* `llm.py:257`: `async def complete(messages: list[dict], system=, max_tokens=, temperature=, **kwargs)`.
* `orchestrator.py:_decompose_task` (from #554) uses the CORRECT form: `complete(messages=[{"role":"user","content":prompt}], system=..., max_tokens=2048)`. Mirror that.
* The aggregator's `_synthesize` is the advisory-synthesis path (optional, LLM-generated summary of subtask results). It's called from `aggregate()` when `self._llm_client` is set + summaries exist. Currently silently fails (returns empty/wrong).

## Requirements

* `aggregator.py:250`: change `complete(prompt=prompt, max_tokens=1024)` → `complete(messages=[{"role":"user","content":prompt}], max_tokens=1024)`.
* Optionally add a `system=` prompt ("You are a result synthesis agent...") — the current prompt has the instruction inline in `prompt`, which is fine as a user message. Keep it simple: just wrap as a user message.
* Test: mock `llm_client.complete`, assert it's called with `messages=[{"role":"user","content": <the prompt>}], max_tokens=1024` (not `prompt=`).

## Acceptance Criteria

* [ ] `complete` called with `messages=` (correct param)
* [ ] Test asserts the call shape
* [ ] `pytest tests/python/test_aggregator.py` passes
* [ ] ruff clean
* [ ] CI green

## Out of Scope

* The advisory-only ADR (already done in #550)
* Multi-turn synthesis

## Technical Notes

* `aggregator.py:250` — the bug line
* `llm.py:257` — the correct signature
* `orchestrator.py` `_decompose_task` (from #554) — the correct usage to mirror
