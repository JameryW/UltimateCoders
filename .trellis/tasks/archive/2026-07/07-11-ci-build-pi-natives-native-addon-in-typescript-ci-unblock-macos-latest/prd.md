# CI: Build pi_natives Native Addon in TypeScript CI (Unblock macos-latest)

## Goal

Unblock the TypeScript CI queue. `ci-typescript.yml` runs on `macos-13`
(GitHub-deprecating, supply-constrained) — all 3 TS jobs queue 30-60+ min
while Rust/Python CI (Linux) completes normally. PR #231 (decomposer
multi-agent steps) is blocked on this.

The blocker on switching to `macos-latest` (arm64): the OMP vendor commits
only `pi_natives.darwin-x64-modern.node`. arm64 has no prebuilt addon →
`loadNative` fails → 760 test failures (verified in closed PR #233).

## What I already know (verified)

- `.github/workflows/ci-typescript.yml` — 3 jobs (`bun-test-mnemopi`,
  `bun-test-coding-agent`, `bun-test-uc-orchestrator`), all `runs-on: macos-13`.
- `vendor/oh-my-pi/packages/natives/native/` — only
  `pi_natives.darwin-x64-modern.node` committed. No arm64, no linux variant.
- `vendor/oh-my-pi/packages/natives/scripts/build-native.ts` — builds the
  cdylib via `cargo` (Rust) into `pi_natives.<platform>-<arch>.node`. For
  non-cross host builds (arm64 on arm64 host), sets
  `RUSTFLAGS=-C target-cpu=native`, no zig/cross needed.
- Build entry: `bun --cwd=vendor/oh-my-pi/packages/natives run build`.
- Loader: `vendor/oh-my-pi/packages/natives/native/loader-state.js` resolves
  `pi_natives.<process.platform>-<process.arch>[<variant>].node`.

## Decisions (locked)

- **D1: switch `runs-on: macos-13` → `macos-latest`** in all 3 jobs.
- **D2: add a Rust toolchain step + native build step before `bun test`** in
  each job: `dtolnay/rust-toolchain@stable` then
  `bun --cwd=vendor/oh-my-pi/packages/natives run build`. Build produces
  `pi_natives.darwin-arm64.node` in-place → loader finds it.
- **D3: no changes to the OMP submodule itself** — build is ephemeral in CI,
  the committed x64 binary stays for local Intel-Mac dev. Only the workflow
  file changes.
- **D4: native build step runs after `bun install`** (build script imports
  via bun) but before `bun test`.

## Assumptions

- `cargo` build of pi-natives on macos-latest arm64 succeeds (napi-rs cdylib,
  no platform-specific deps that break on arm64 — to verify on first CI run).
- Build time acceptable (~1-2 min) vs queue savings.

## Acceptance Criteria

- [ ] `ci-typescript.yml` all 3 jobs `runs-on: macos-latest`.
- [ ] Each job installs Rust + runs `bun --cwd=.../natives run build` before tests.
- [ ] TS CI jobs leave `queued` state and run to completion.
- [ ] PR #231's TS CI passes (decomposer-steps unblocked).
- [ ] No change to committed native binaries or the OMP submodule.

## Out of Scope

- Cross-compiling linux variant (TS CI stays macOS-only; matches dev env).
- Prebuilt arm64 binary committed to submodule (upstream OMP decision).
- Refactoring the 3 near-identical jobs into a matrix (separate cleanup).

## Technical Approach

Per job, insert after `bun install --frozen-lockfile`:
```yaml
      - uses: dtolnay/rust-toolchain@stable
      - run: bun --cwd=vendor/oh-my-pi/packages/natives run build
```
Then `runs-on: macos-latest`. The build writes
`pi_natives.darwin-arm64.node` next to the committed x64 file; loader picks
the arch-matching one.
