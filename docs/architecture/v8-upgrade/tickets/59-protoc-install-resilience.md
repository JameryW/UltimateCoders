# 59 — The protoc install steps are gone, not hardened (T59)

**What happened.** On 2026-11-11 the runner's configured Ubuntu mirror stopped resolving
(`Temporary failure resolving 'azure.archive.ubuntu.com'`) and **all seven** `Install
protoc` steps failed — six in Rust CI, one in Python CI — before a single test ran. PR #908
sat red through three retries with a change unrelated to it, while the full suite had
already passed locally.

**Measured, not assumed** (from the failed job logs):

- `protoc` is **not** preinstalled on the `ubuntu-24.04` runner image, so an
  "already installed?" short-circuit does not help — the step had to succeed on its own;
- the outage was **persistent**, not a blip: three attempts all failed;
- naive retries make it **worse**: `Acquire::Retries=3` × `Timeout=30` turned a ~7 minute
  red build into a **~30 minute** one (attempt 1: 14:54:58 → failure 15:04:23).

**Root finding — the steps were already redundant.** `crates/uc-grpc/build.rs` sets
`PROTOC` from `protoc-bin-vendored` (a build dependency of that crate) whenever `PROTOC` is
unset:

```rust
if std::env::var_os("PROTOC").is_none() {
    std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
}
```

`uc-grpc` is the only crate that compiles protos, and the vendored binary exists precisely
so a clean machine needs no system compiler. CI has been installing a compiler that the
build never consults.

**Fix: delete all seven steps.** Not "harden the install" — remove the dependency:

- no apt, no Ubuntu mirror, no download, no third-party action, no pin/checksum decision;
- seven fewer steps; a whole class of outage can no longer redden the build.

**Verification.** Both workflow files parse; every job keeps its steps and its sequence
(`lint` 4, `dashboard` 5, `test` 7 in Python CI; 3–9 per Rust job). The Rust and Python CI
jobs themselves are the test: they build `uc-grpc` (and its PyO3 extension) from scratch on
a runner with no system protoc. *Locally `cargo check` cannot be run in this environment —
crates.io is unreachable here (SSL error) — so CI is the only execution evidence, and it is
the same build the steps were blocking.*

**Status:** implemented (`.github/workflows/ci-python.yml`, `.github/workflows/ci-rust.yml`)

**Reversal condition:** if CI's build step ever reports `protoc` missing, the vendored
binary is the thing to inspect — not the workflow.
