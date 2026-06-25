# Replace TUI Frontend with OMP

## Goal

Eliminate the standalone Ink/React TUI (`tui/`) and use oh-my-pi's built-in terminal UI as the sole user-facing frontend. The UC Orchestrator already runs as an omp extension — the user should interact with it through omp's TUI directly, not through a separate gRPC-connected Ink app.

## What I already know

* **Current TUI** (`tui/`): 12 Ink/React components, gRPC client connecting to Rust server at :50051, 103+ unit tests, full task lifecycle UI (submit, watch, cancel, pause, resume)
* **OMP** (`vendor/oh-my-pi/`): Full coding agent platform with its own TUI, extension API, 32+ built-in tools
* **UC Orchestrator** (`packages/uc-orchestrator/`): Already an omp extension — registers `/uc` slash command + LLM tools, uses omp's `runSubprocess` for agent execution
* **Launch**: `run-omp.sh` starts omp with UC extension loaded — this already works
* **Dual TUI problem**: Two independent TUIs exist (Ink TUI + omp TUI), cannot be used simultaneously on the same task
* **gRPC bridge**: TUI → Rust gRPC → NATS → OMP; OMP → GrpcBridge (HTTP+JSON) → Rust gRPC. Indirect, complex.
* **uc-rpc-server.ts**: JSONL stdio bridge for Python OmpBridge — alternative communication path

## Assumptions (temporary)

* The omp TUI already provides adequate terminal UX (input, output, scrolling, CJK)
* The `/uc` slash commands in omp are sufficient for task control (submit/status/cancel/pause/resume)
* The Rust gRPC server is still needed for distributed scenarios but not for local single-user use
* The `tui/` directory can be deleted entirely once OMP replaces it

## Open Questions

* What UC-specific TUI features must be preserved in the omp experience? (subtask tree, worker panel, task switching, bookmarks, search, command palette)
* Should the Rust gRPC server still be started for local use, or can we go pure omp?
* How to handle the offline fallback that the Ink TUI currently provides?

## Requirements (evolving)

* User launches `run-omp.sh` (or equivalent) and gets full UC task orchestration through omp's TUI
* `/uc submit`, `/uc status`, `/uc cancel`, `/uc pause`, `/uc resume` work as today
* Subtask progress is visible within omp's output stream
* No separate Ink TUI process needed

## Acceptance Criteria (evolving)

* [ ] `run-omp.sh` provides complete UC task lifecycle without any separate TUI
* [ ] `tui/` directory is removed or archived
* [ ] All UC control operations work through omp slash commands
* [ ] Subtask status/progress is visible in omp output

## Definition of Done

* Tests added/updated for any changed orchestrator code
* Lint / typecheck / CI green
* Docs updated (README, run instructions)
* `tui/` removed or clearly marked as deprecated

## Out of Scope (explicit)

* Distributed/multi-user scenarios (still need gRPC server for those)
* New omp UI features (subtask tree overlay, etc.) — use what omp already provides
* Python agent layer changes

## Technical Notes

* OMP extension API: `pi.registerCommand()`, `pi.ui.notify()`, `ctx.ui.notify()`, `pi.runSubprocess()`
* Current `/uc` commands already use `ctx.ui.notify()` for feedback — this renders in omp's TUI
* The Ink TUI's gRPC client (`tui/src/grpc/`) and hooks (`tui/src/hooks/`) become unnecessary
* The Rust gRPC server + proto definitions remain for distributed use — just not required for local
