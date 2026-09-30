# Deployment observations

Baseline: `9a8a43e0`; clean checkout before task creation.

On 2026-09-30, Docker Desktop startup failed before creating its engine pipe. The current backend log identifies the cause as failure to remove `%LOCALAPPDATA%/Docker/run/sailor-ingest.sock` (Windows error 1920). The file is a reparse point. Ubuntu-24.04 and docker-desktop WSL2 distributions exist; vmcompute is running. Investigate the temporary runtime socket without resetting Docker data.

No provider API-key environment variables or local model listeners were found in the initial Windows process environment/port inventory. WSL can read an existing Docker env file that the Windows sandbox could not inventory; its values have not been printed. User selected local Ollama for testing. Windows Ollama 0.35.0 is installed with a tool-capable `qwen3.8-9b-uc:latest` Q4_K_M model already downloaded.

## Findings and fixes in progress

- WSL Ubuntu has a working native Docker 29.1.3 engine and existing UC images/volumes. Docker Desktop remains blocked by a second stale socket (`docker-secrets-engine/engine.sock`); its ordinary rename/delete fails. Native WSL Docker avoids any reset of Desktop state.
- Historical cache ACLs prevent Docker's context walker from reading the Windows checkout despite `.dockerignore`. Build from a Git archive on the WSL filesystem instead; preserve caches.
- Clean checkout builds fail because both Dockerfiles COPY Cargo.lock, but `.gitignore` excludes it. Track the workspace lock file.
- Worker installer required missing unzip; resolved by copying the official pinned Bun 1.4.2 binary. The older Bun 1.3.9 also failed OMP syntax checks.
- WSL already runs a host PostgreSQL on 5432. Add a configurable published PostgreSQL port; container-to-container traffic remains on 5432.
- Noninteractive pnpm needed CI=true to reconcile existing dependency metadata. Dashboard build/lint and all four tests pass.
- Full Python suite: 1335 passed, 1 skipped, 8 integration tests deselected. Rust workspace check passes. Rust tests pass with escalated temp-directory access (456 engine unit tests plus 5 context, 226 gRPC unit, 3 roster, 8 gRPC integration and 50 type tests); infrastructure-dependent ignored tests still need live execution.
- Windows Python environment's tiktoken compiled extension is missing; reinstall a Python 3.14-compatible wheel before real local-model calls.


Final functional evidence and reproduction: `docs/local-deployment-verification.md`. Production app runs at port 8081; real Ollama coding, GPU fixed-prompt Oracle benchmark, live storage, controls, scheduling and restart recovery were exercised. All discovered cross-layer defects received focused regressions. Final quality counts are recorded in the task verification report.
