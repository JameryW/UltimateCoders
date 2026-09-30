# Local deployment and comprehensive functional verification

## Source and goal

User request: deploy locally, comprehensively verify functionality, and fix all discovered problems. Continue from the completed MetaInfer integration and README work at `9a8a43e0`.

## Acceptance criteria

- Build and run the production Docker app locally; verify service health and startup diagnostics.
- Exercise Dashboard UI and API, Gateway, NATS dispatch, Worker execution, task state/events/control, search, and Memory against running services.
- Exercise restart/recovery and offline/error behavior, using isolated fixture repositories for mutation.
- Verify the optional inference domain through its public seams; record whether external MetaInfer and GPU experiments were actually available. Do not claim fixture measurements as GPU evidence.
- Fix discovered code defects with focused regression tests, then run relevant complete Python/Rust/frontend checks.
- Persist reproducible commands, observations, fixes, and remaining externally blocked checks. Update deployment guidance where evidence changes its contract.

## Decisions

- Use an independent task and `codex/local-deployment-verification` branch. Preserve other chats' task pointers and edits.
- Start and repair local services within the user's deployment authorization; preserve Docker data, credentials, and existing repositories.
- No remote GitHub tracker mutations or pushes are requested. Tracker authentication is available; this local verification request is recorded here.
- Derive provider and inference availability from existing configuration before requesting missing credentials or experiments.

## Validation matrix

| Area | Evidence |
| --- | --- |
| Production build/start | Compose build, health, logs, ports |
| Dashboard | Browser navigation, REST, SSE, static/proxy/offline |
| Distributed execution | Submit, dispatch, contract/capabilities, terminal state, cancellation |
| Storage/search | CRUD, repository indexing, hybrid retrieval, persistence |
| Failure/recovery | Backend interruption and restart, retained state/events |
| Inference | Adapter, sandbox cancellation, benchmark/Oracle/rollback, optional real external run |
| Quality | Python, Rust, frontend checks, independent reviews |
