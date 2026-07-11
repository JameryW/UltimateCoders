# Route Multi-Agent Step Subtasks to Capable Workers

## Background

PR #231 closed the TS-layer gap: decomposer now emits 3-step chains
(claude-code → codex CR → claude-code revise), and `steps` flow end-to-end
to the worker's `_execute_steps`. But a correctness gap remains in
**dispatch routing**: the scheduler routes subtasks to workers by
`requiredCapabilities`, yet the decomposer's step declarations do NOT
declare capabilities matching each step's agent. A subtask whose steps
include `agent="codex"` can be routed to a worker/sandbox without the codex
CLI, causing the CR step to fail at subprocess time (`command not found`),
aborting the whole chain.

## What I already know (verified this session)

- `packages/uc-orchestrator/src/agents/decomposer.md` — emits `steps[].agent`
  with values "claude-code"/"codex" (3 codex mentions), but **0 mentions of
  `requiredCapabilities`/`capabilities`**. Steps and caps are not linked.
- `packages/uc-orchestrator/src/orchestrator/scheduler.ts:534-542` — routes
  subtasks to workers by `requiredCapabilities` (ALL must match). No derivation
  from `steps[].agent`.
- `python/ultimate_coders/agent/worker.py:1102` — `_execute_steps` calls
  `self._sandbox_manager.execute(..., agent=step.agent)` per step.
- `python/ultimate_coders/agent/sandbox.py:198-205` — `_create_adapter(agent)`
  returns `ClaudeCodeAdapter()`/`CodexAdapter()` by name; raises `ValueError`
  only for UNKNOWN agents (not for missing CLI). Adapter creation succeeds even
  if the CLI isn't installed.
- `CodexAdapter.build_request` (sandbox.py:1043) — emits `command: "codex"`.
  If `codex` CLI absent in the execution sandbox, the subprocess fails.
- Execution backend: Python sandbox delegates to Rust `DockerSandbox`
  (`engine.execute_in_sandbox`) which runs agent commands in
  `ultimate-coders/sandbox:latest` containers. So the CLI must exist in the
  **sandbox image**, not the worker host.

## The gap (two layers)

1. **Dispatch routing (orchestrator)**: decomposer doesn't derive
   `requiredCapabilities` from `steps[].agent`. A codex-step subtask can land
   on a worker whose sandbox image lacks codex.
2. **Worker capability advertisement**: workers register capabilities
   (`_derive_capabilities` from SandboxConfig tools/mcp) but NOT which agent
   CLIs their sandbox image has. The scheduler can't match "codex" because
   workers don't advertise it.

## Open Questions (RESOLVED by research)

Research (research/sandbox-image-clis.md) confirmed:
- **Q1: NO sandbox image exists.** `ultimate-coders/sandbox:latest` is a hardcoded
  Rust constant (docker.rs:21) that nobody builds. No Dockerfile produces it.
- **Q2: DockerSandbox is dead code.** Production always uses `SubprocessSandbox`
  (Python worker hardcodes `backend="subprocess"` at nats_worker.py:834;
  LocalEngine uses SubprocessSandbox). `UC_SANDBOX_MODE` is a dead config knob.
  So the real execution path is: **Python worker runs `claude`/`codex` from the
  worker host PATH** via `create_subprocess_exec` (sandbox.py:430).
- **Q3: `_derive_capabilities` does NOT probe CLIs.** Worker advertises
  `["code","search","memory","test","decompose","review"]` unconditionally
  (worker.py:316) — derived from SandboxConfig fields + env, never `shutil.which`.
  No capability reflects whether `claude`/`codex` is on PATH.

**Conclusion:** Only layer-2 fix matters (no image build). The bug is real: a
codex-step subtask routes to a worker whose PATH may lack `codex`, and the
scheduler can't tell because workers don't advertise agent-CLI presence.

## Decisions (locked)

- **D1 (derive caps from steps)**: orchestrator derives `requiredCapabilities`
  from `steps[].agent` — union of step agents (e.g. `["claude-code","codex"]`)
  merged with any explicit caps, so a 3-step chain requires a worker advertising both.
  Implement in `parseSubtaskOutput` (TS) — the function already reads steps.
- **D2 (worker probes CLIs)**: `worker.py:_derive_capabilities` probes
  `shutil.which("claude")`/`shutil.which("codex")` and advertises `"claude-code"`/
  `"codex"` capabilities when present. This makes D1's requirements matchable.
- **D3**: capability names standardize on the adapter names (`claude-code`,
  `codex`) — matching `step.agent` values and `_create_adapter`'s accepted names.
- **Out of scope**: building the dead `ultimate-coders/sandbox:latest` image;
  reviving DockerSandbox; removing the dead `UC_SANDBOX_MODE` knob (separate cleanup).

## Acceptance Criteria

- [ ] `parseSubtaskOutput` (orchestrator.ts) derives `requiredCapabilities` from
      `steps[].agent` (union, deduped, merged with explicit caps).
- [ ] `worker.py:_derive_capabilities` advertises `claude-code`/`codex` when
      `shutil.which` finds the CLI on PATH.
- [ ] Test: subtask with codex step → requiredCapabilities includes "codex".
- [ ] Test: worker with codex on PATH advertises "codex"; without it, doesn't.
- [ ] Existing tests green (no regression in capability matching / step execution).

## Technical Approach

1. **orchestrator.ts `parseSubtaskOutput`**: after mapping steps, if `def.steps`
   non-empty, derive `requiredCapabilities = [...explicit, ...new Set(steps.map(s=>s.agent))]`.
   (Steps use "claude-code"/"codex" — same as adapter names.)
2. **worker.py `_derive_capabilities`**: append `shutil.which("claude")` → "claude-code",
   `shutil.which("codex")` → "codex" to the derived list. Best-effort — missing CLI
   = capability not advertised (so D1's required cap excludes that worker).
3. Tests: TS unit test for cap derivation from steps; Python test for CLI-probe caps
   (mock shutil.which).
