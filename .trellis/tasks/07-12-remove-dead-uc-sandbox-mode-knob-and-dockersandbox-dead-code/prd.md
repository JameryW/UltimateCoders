# Remove Dead UC_SANDBOX_MODE Knob and DockerSandbox Dead Code

## Background

Task `07-12-route-multi-agent-step-subtasks-to-capable-workers` (PR #236,
merged `3916e0aa`) researched the sandbox execution path and documented two
dead artifacts as **Out of scope (separate cleanup)**:

> Out of scope: building the dead `ultimate-coders/sandbox:latest` image;
> reviving DockerSandbox; removing the dead `UC_SANDBOX_MODE` knob
> (separate cleanup).

This task is that separate cleanup.

## What I already know (verified this session)

- `crates/uc-engine/src/sandbox/docker.rs` (440 lines) — defines `DockerSandbox`
  struct + `Sandbox` impl. Hardcodes `DEFAULT_SANDBOX_IMAGE =
  "ultimate-coders/sandbox:latest"` (docker.rs:21). No Dockerfile produces
  this image (confirmed in prior research `sandbox-image-clis.md`).
- `DockerSandbox` is re-exported at `crates/uc-engine/src/lib.rs:76` but
  **never instantiated** anywhere in the workspace (grep `DockerSandbox::`
  / `DockerSandbox()` → 0 hits outside docker.rs/mod.rs themselves). No
  Python binding (`uc-python` has no `DockerSandbox` export).
- Production always uses `SubprocessSandbox`:
  - `python/ultimate_coders/nats_worker.py:834` hardcodes
    `backend="subprocess"`.
  - `python/ultimate_coders/agent/sandbox.py:56` default `backend="subprocess"`.
- `UC_SANDBOX_MODE` env var: **no Python or Rust code reads it**
  (`grep -rn UC_SANDBOX_MODE` in `.py`/`.rs` → 0 hits). It appears only as
  `UC_SANDBOX_MODE: "subprocess"` in `docker/docker-compose.yml:245,290`
  (two worker-service env blocks) — a dead knob that sets nothing.
- `sandbox.py:35` docstring still lists `"subprocess" or "docker"` as valid
  backends; `SandboxConfig.backend` field accepts `"docker"` but nothing
  routes to a Docker backend on the Python side (no DockerSandbox in Python).

## The gap

Two pieces of dead code mislead readers into thinking a Docker sandbox path
exists and is selectable via `UC_SANDBOX_MODE`:

1. **Rust**: `docker.rs` (440 LOC) + its `lib.rs` re-export + mod.rs mention.
   Never built, never instantiated, image never produced.
2. **Compose + docs**: `UC_SANDBOX_MODE` env knob in two worker services —
   read by no code. Plus `sandbox.py` docstring/field implying a `"docker"`
   backend option that has no implementation.

## Decisions (locked)

- **D1**: Delete `crates/uc-engine/src/sandbox/docker.rs` entirely. Remove
  `pub use sandbox::docker::DockerSandbox;` from `lib.rs:76`. Remove the
  `mod docker;` declaration in `sandbox/mod.rs`. Update mod.rs module docs
  that reference DockerSandbox as the production backend.
- **D2**: Remove `UC_SANDBOX_MODE: "subprocess"` from both worker-service
  env blocks in `docker/docker-compose.yml` (lines 245, 290). Add a one-line
  `# removed: UC_SANDBOX_MODE` breadcrumb? **No** — deletion over breadcrumb;
  git history suffices.
- **D3**: `sandbox.py:35` docstring → drop `"or "docker""` so it reads
  `backend: Sandbox isolation backend ("subprocess").`. Leave the
  `SandboxConfig.backend` field as-is (still a str, default "subprocess") —
  removing the field is a broader API change, out of scope here. Just stop
  advertising a backend that doesn't exist.
- **Out of scope**: removing `SandboxConfig.backend` field entirely;
  reviving a real Docker/NsJail backend; the `TestWorkerSandboxMode` test
  class in `tests/python/test_sandbox.py:519` (keep — it tests the field's
  default, which still exists under D3).

## Acceptance Criteria

- [ ] `crates/uc-engine/src/sandbox/docker.rs` deleted; no `DockerSandbox`
      symbol remains in `crates/` (grep clean outside git history).
- [ ] `lib.rs` no longer re-exports `DockerSandbox`; `sandbox/mod.rs` no
      longer declares `mod docker` or references it in docs.
- [ ] `docker/docker-compose.yml` has zero `UC_SANDBOX_MODE` occurrences.
- [ ] `sandbox.py:35` docstring no longer mentions `"docker"` as a backend.
- [ ] `cargo check -p uc-engine` green; `cargo test -p uc-engine` green.
- [ ] Python tests green (`tests/python/test_sandbox.py` in particular).
- [ ] ruff lint green; TS CI green (no TS touch expected, but verify).

## Technical Approach

1. **Rust**: `git rm crates/uc-engine/src/sandbox/docker.rs`. Edit
   `sandbox/mod.rs`: remove `mod docker;` (or `pub mod docker;`), drop the
   `DockerSandbox` line from the module-level doc list. Edit `lib.rs:76`:
   remove the `pub use`. Run `cargo check -p uc-engine` — fix any leftover
   references (expect none per grep).
2. **Compose**: edit `docker/docker-compose.yml`, delete the two
   `UC_SANDBOX_MODE: "subprocess"` lines.
3. **Python docstring**: one-line edit to `sandbox.py:35`.
4. Verify: `cargo check`, `cargo test -p uc-engine`, `ruff check`, run
   `tests/python/test_sandbox.py`.

## Risk

- **API surface**: `DockerSandbox` is `pub` re-exported. Removing it is a
  semver-breaking change for any external consumer of `uc-engine`. This
  repo is pre-1.0 and the symbol is documented as dead — acceptable break.
  Note in commit message.
- **No runtime regression**: DockerSandbox is never constructed, so deletion
  cannot affect any live path. The only risk is a missed reference grep
  didn't catch; `cargo check` catches it.
