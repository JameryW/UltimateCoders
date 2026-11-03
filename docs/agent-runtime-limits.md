# Agent runtime limits

Four worker-side limits can abort a long content phase even when the plan is valid. Each is now
configurable without patching the worker.

## Stream reader line limit

The sandbox reads agent stdout line by line. Claude Code's `--output-format stream-json` can emit a
single JSON line far larger than asyncio's 64 KiB `StreamReader` default (large tool results do this),
which aborts the step with:

```
Separator is found, but chunk is longer than limit
```

`UC_STREAM_LIMIT_BYTES` sets the limit (default `67108864`, i.e. 64 MiB). Invalid or non-positive
values fall back to the default with a warning.

```bash
UC_STREAM_LIMIT_BYTES=134217728   # 128 MiB for unusually large tool payloads
```

## Claude Code turn budget

The Claude Code adapter passes `--max-turns`. It now reads `max_turns` from the merged agent config
instead of hardcoding 20; the default stays 20 when the key is absent or invalid.

```json
{"agent": "claude-code", "prompt": "...", "agentConfigJson": "{\"max_turns\": 200}"}
```

## Default subtask timeout

`Worker` uses `subtask.timeout_seconds` when set, otherwise a deployment default that can be raised
with `UC_SUBTASK_TIMEOUT_SECONDS` (default `600`). Long evidence-collection or packaging phases
routinely exceed ten minutes.

```bash
UC_SUBTASK_TIMEOUT_SECONDS=3600
```

A per-subtask `timeout_seconds` still wins over the environment default.

## Description keyword profiles

`Worker._match_subtask_template` maps subtask-description keywords (`review`, `audit`, `analyze`,
`inspect`, ...) to agent profiles. The review profile is read-only, and it applies to the whole
subtask — so a multi-step plan whose description happens to contain `review` cannot run a
write-capable remediation step.

`UC_DISABLE_DESCRIPTION_PROFILES=1` disables the heuristic entirely; explicit capabilities and
per-step agent configs still apply.

```bash
UC_DISABLE_DESCRIPTION_PROFILES=1
```

## Tests

- `tests/python/test_sandbox.py::TestAgentRuntimeLimits` — stream limit, Claude turn budget.
- `tests/python/test_worker_capabilities.py::TestRuntimeDefaults` — subtask timeout, description
  profile opt-out.
