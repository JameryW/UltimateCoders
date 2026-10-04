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
