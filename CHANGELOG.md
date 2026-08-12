# Changelog

## [0.2.1] - 2026-08-11

### Added

- Shared UC command parsing and execution for Dashboard and TUI task actions.
- TUI command bar for submitting, inspecting, pausing, resuming, and cancelling tasks through the real backend.

### Fixed

- Hardened dashboard gRPC-Web and TUI WebSocket proxying for long-lived streams.
- Improved worker runtime dependency coverage for Claude Code and Codex execution.
- Kept task submission and task-control feedback connected to the real TaskService path instead of demo-only state.
