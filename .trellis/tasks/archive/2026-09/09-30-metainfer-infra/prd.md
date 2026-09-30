# MetaInfer inference infrastructure domain

## Goal

Implement the approved attached plan through a working optional domain backend. Tracker authority: `.scratch/metainfer-infra/issues/01-inference-domain.md`.

## Requirements and acceptance

1. Typed MetaInfer tool interface for porting, kernel/runtime optimization, trace analysis and benchmark execution, backed by verified upstream form/task endpoints.
2. Shared benchmark results and Oracle reject incorrect, unverifiable or regressed results.
3. A bounded optimization workflow records hypotheses/evidence, keeps improvements, restores rejected changes and honors cancellation.
4. InfraAgent routing integrates with current orchestration, worker capability advertising and sandbox adapter registration; explicit overrides win.
5. Typed execution/adaptation graphs and benchmark histories persist as artifacts and accepted UC memory.
6. Docker/env documentation and public-seam tests cover the end-to-end configuration and failure boundaries.

## Confirmed facts

GitHub authentication is unavailable, so the repository's local tracker fallback applies. The inherited task pointer belongs to another session; this chat uses its own TRELLIS_CONTEXT_ID. Baseline commit is `893e4086`. Python tests/lint are available in `.venv/Scripts/python.exe`.

## Scope

No MetaInfer vendoring, global planner replacement, WebUI integration or GPU provisioning. Real GPU validation requires an external service and shared workspace; automated tests use contract-faithful HTTP fixtures and local git workspaces.

## Authorization

The user's instruction to proceed according to the attached implementation plan authorizes implementation and necessary reversible local work. No additional discovery approval is needed for these implementation choices.
