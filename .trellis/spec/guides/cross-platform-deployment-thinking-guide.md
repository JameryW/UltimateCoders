# Cross-Platform Deployment Thinking Guide

> **Purpose**: Keep local development, Docker, and Windows/Unix launch paths behaviorally aligned.

## Before Changing a Local Deployment Path

- [ ] Check both virtual-environment layouts: `.venv/bin` and `.venv/Scripts`.
- [ ] Check both command execution models: POSIX shell commands and Windows `cmd.exe`.
- [ ] Normalize executable shell scripts to LF in Docker images when their build context can originate on Windows; an executable CRLF shebang resolves to `/bin/sh\r` and fails with `not found` on Linux.
- [ ] Before recursively changing ownership of a bind-mounted workspace, test whether the runtime user can already write to it. Docker Desktop mounts are often writable despite host UID differences; an unconditional `chown -R` can leave Dashboard/TUI unavailable while it traverses a large repository.
- [ ] When enforcing a subprocess timeout on Windows, put the command in a Job Object so a shell wrapper and every descendant terminate together; never wait for inherited output pipes on the timeout return path.
- [ ] Check whether a path is a real repository root before inheriting Git metadata from a parent repository.
- [ ] Check cleanup behavior on Windows, where open handles and read-only Git files can block deletion.
- [ ] Check that every SQLite-backed component exposes a close path for its thread-local connection.

## Docker UI and API Proxy Contract

The dashboard UI image must satisfy two independent contracts:

1. It starts without `orchestrator` or `gateway` DNS entries, so the frontend can render its offline state.
2. On the Compose network, `/dashboard/api/`, `/ws/tui`, and gRPC-Web requests resolve the service names at request time and reach the backend.

For Nginx upstreams that may be unavailable when the image starts:

- Do not use a statically resolved hostname in a way that prevents Nginx from starting.
- Use a variable-backed upstream and Docker's embedded DNS resolver (`127.0.0.11`).
- Verify both standalone static routes and Compose-network API proxy routes.

## Regression Test Matrix

| Boundary | Required check |
|----------|----------------|
| Python launcher | `.venv/bin/python` and `.venv/Scripts/python.exe` resolution |
| Verification command | Quoted arguments and `true`/`false` on Windows |
| Sandbox timeout | `cmd.exe` descendants terminate with the wrapper and the timeout result returns promptly |
| Git metadata | Nested non-repository directories do not inherit outer remote data |
| SQLite lifecycle | Close metrics/alert stores before removing temporary databases |
| Dashboard image | Standalone startup, SPA fallback, static assets, API proxy |

## After Fixing a Deployment Bug

- [ ] Add a focused regression test when the issue is code-level.
- [ ] Run the full Python suite and frontend quality checks.
- [ ] Build the production image and test it both without a backend and on the Compose network.
- [ ] Record the contract in the relevant project spec or guide.
