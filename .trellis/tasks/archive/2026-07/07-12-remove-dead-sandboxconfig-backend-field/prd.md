# Remove Dead SandboxConfig.backend Field

## Background

PR #237 (merged `514ec3bf`) removed the dead `DockerSandbox` and
`UC_SANDBOX_MODE` knob. Its PRD explicitly left one item as out-of-scope:

> Out of scope: removing `SandboxConfig.backend` field entirely [is a]
> broader API change, out of scope here. [D3 only updated the docstring.]

This task is that follow-up — remove the now-fully-dead `backend` field.

## What I already know (verified this session)

- `python/ultimate_coders/agent/sandbox.py:56` — `backend: str = "subprocess"`
  field on `SandboxConfig`. No code reads it to select a backend.
- `sandbox.py:35` — docstring `backend: Sandbox isolation backend ("subprocess").`
  (already cleaned of "docker" by PR #237).
- `python/ultimate_coders/nats_worker.py:834` — the ONLY caller passing
  `backend="subprocess"` (== the default). Removing the field removes this arg.
- **No backend-selection logic exists**: grep `\.backend` / `backend=` in
  `sandbox.py`/`worker.py`/`nats_worker.py` → only the field def + the one
  caller. No `if config.backend == ...` branch anywhere. The field is written
  but never read.
- Tests referencing the field:
  - `tests/python/test_sandbox.py:35` — `assert config.backend == "subprocess"`
  - `tests/python/test_sandbox.py:49` — `backend="docker"` in `test_custom`
  - `tests/python/test_sandbox.py:56` — `assert config.backend == "docker"`
  - `tests/python/test_sandbox.py:519` — `TestWorkerSandboxMode` class name
    contains "SandboxMode" but its body does NOT reference the `backend`
    field (only checks `worker._sandbox_manager is not None`). Keep the class;
    do not rename.

## The gap

`SandboxConfig.backend` is a vestigial field: written in one place
(nats_worker default), asserted in tests, but never read to choose a
backend. After PR #237 there is exactly one backend (`SubprocessSandbox`),
so the field can only ever hold `"subprocess"`. It misleads readers into
thinking backend selection is configurable. Dead field → delete.

## Decisions (locked)

- **D1**: Remove `backend: str = "subprocess"` field from `SandboxConfig`
  (`sandbox.py:56`) and its docstring line (`sandbox.py:34-35`).
- **D2**: Remove `backend="subprocess"` kwarg from the `SandboxConfig(...)`
  call at `nats_worker.py:834`.
- **D3**: Remove the three field references in `tests/python/test_sandbox.py`
  (lines 35, 49, 56). Keep `TestWorkerSandboxMode` class as-is (name is
  cosmetic; body unaffected).
- **Out of scope**: renaming `TestWorkerSandboxMode`; auditing other
  SandboxConfig fields; the Rust-side `Sandbox` trait backends (already
  cleaned in PR #237).

## Acceptance Criteria

- [ ] `SandboxConfig` has no `backend` field (grep `backend` in
      `sandbox.py` → 0 hits).
- [ ] `nats_worker.py` does not pass `backend=` to `SandboxConfig`.
- [ ] `tests/python/test_sandbox.py` has no `config.backend` / `backend=`
      references (the `repo_id="backend"` string-literals at lines 1557+ are
      unrelated test data — leave them).
- [ ] `ruff check python/ tests/` green.
- [ ] `tests/python/test_sandbox.py` passes (all classes).
- [ ] `maturin develop` / import works (no missing-field errors at call sites).

## Technical Approach

1. `sandbox.py`: delete the `backend` field line + its docstring `Args:` line.
2. `nats_worker.py:834`: delete `backend="subprocess",` from the
   `SandboxConfig(...)` call.
3. `test_sandbox.py`: delete the 3 references (lines 35, 49, 56).
4. Verify: `ruff check`, `pytest tests/python/test_sandbox.py`, and a
   smoke `python3 -c "from ultimate_coders.agent.sandbox import SandboxConfig; SandboxConfig()"`.

## Risk

- **Low**: the field is never read, so deletion cannot change runtime
  behavior. The only risk is a missed call site passing `backend=`; grep +
  import smoke test catch it. Py3.9 dataclass tolerates removing a field
  with a default (no positional-arg breakage since the one caller uses kwarg).
