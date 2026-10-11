# Design

Gateway owns control transitions; snapshots are projections. Add a dedicated
retry command, use the current dispatch attempt as the optimistic fence, and
route OMP retries through it. Parent pause/cancel and terminal child states
cannot be undone by a generic snapshot. Retain persisted attempt counters.

The remote-job adapter negotiates an explicit UC service contract for workspace
challenges and all-writer quiescence. Legacy MetaInfer kill acknowledgements
remain usable as stop requests but cannot authorize cleanup. Remote-state
reconciliation validates identity, expected record version and service evidence
before releasing ownership; every change is audited.

RuntimeState remains the transactional seam for SQLite and PostgreSQL. Add
indexed metadata, bounded queries and typed domain operations rather than
making callers scan every JSON document. Capacity allocation and wait ordering
must be atomic. Delivery uses renewable claims, bounded retries and compact
delivered tombstones to preserve dispatch deduplication indefinitely.

Artifact content is identified by SHA-256, published through a shared durable
store so Dashboard downloads do not depend on the worker's local volume.
Workspace challenge verification must happen before any remote launch.

Benchmark evidence separates immutable environment identity from changing
measurement conditions. Resolve actual GPU UUIDs and hold shared device
reservations from baseline through final candidate validation. Runtime/model
identity participates in comparison; temperatures/clocks/load are recorded.

Real integration is opt-in with pinned service revision and actual hardware
evidence. Unsupported service capabilities fail closed and are visible to
operators. Existing CPU/Ollama tests retain their explicitly limited scope.

## Service extension continuation (2026-10-11)

Deploy an additive UC contract module alongside the pinned upstream. Adapt its
existing Launcher interface; do not copy or edit upstream task implementations.
Each launch uses a new, immutable OCI container with private PID/cgroup
namespaces, dropped capabilities, no privilege escalation, read-only root and
only the assigned worktree, task state and generated workspace writable.
Uncontained upstream mutation/restart/cluster launch endpoints are disabled.

Persist launch intent before container creation, container identity before
start, and kernel cgroup identity before releasing a launch gate. Unknown
create/start/stop outcomes remain unresolved. A service restart observes the
same container and record; it never repeats start. Completion evidence
requires verified container identity, stopped OCI state and an empty/missing
owned cgroup. An absent record, foreign container, running descendant or
unavailable runtime cannot produce a stopped-writer receipt. Persist hashed
proofs for operator inspection. Podman permits local WSL verification while
the same CLI adapter supports Docker deployments.

Tests use the agreed HTTP contract and Launcher/OCI lifecycle interfaces.
Real containment checks include a detached writer, service restart and stop;
they are recorded separately from unit fixtures and specialized GPU acceptance.
