# Evidence

- CI clippy job is lib-only: `cargo clippy --workspace -- -D warnings`
  (`.github/workflows/ci-rust.yml:79`). Without `--tests`/`--all-targets`,
  `#[cfg(test)]` code is never linted.
- That is exactly how 3 of the 4 warnings fixed in `ab505c59` survived:
  the `unit_arg` sites at `server.rs:9073/9120/9155` are all inside
  `mod tests` — invisible to the CI gate under every toolchain.
- The 4th (`:3717`, lib code) stayed green in CI for a different reason:
  the line dates to T7 (`b15c4c82`, 2026-09-14) yet ci-rust runs on 09-16 /
  09-17 / 09-18 are all success with `default = []` (line compiles). The
  `let_unit_value` lint postdates CI's toolchain at the time (local
  1.97.0 documents it). Toolchain drift, not a config hole — recorded,
  not acted on.
- `cargo clippy --workspace --all-targets` on this tree (post-`ab505c59`):
  **zero warnings**. The gate extension is green on arrival.
- No repo guard script shells out to cargo (scripts/ guards are Python
  census tools); clippy ownership already lives in `ci-rust.yml`. A local
  guard wrapper would add maintenance for zero new coverage.
