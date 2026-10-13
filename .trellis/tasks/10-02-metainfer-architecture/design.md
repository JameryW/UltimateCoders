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

## Repository candidate publication seam (2026-10-12)

The pinned evolve-kernel orchestrator ranks code in its generated workspace;
UC's independent Oracle measures the assigned repository. An additive execution
wrapper must publish the ranked candidate inside the same OCI writer scope,
before that scope stops. The wrapper copies code bytes only; it never executes
generated code on the service host or commits Git changes. Publication is not
Oracle acceptance.

Use no-follow directory descriptors for the repository-relative target, reject
Git metadata, symlink paths, nonterminal upstream runs and unmeasured/invalid
libraries, then replace the owned kernel atomically and record its SHA-256.
The UC protected harness and Oracle still decide correctness, improvement and
rollback. Image capability and rollout must be explicit; old images must not
silently claim repository publication.

## Explicit GPU/native Ollama profile (2026-10-12)

Use a trusted image-owned GPU/model probe with no model-generated execution.
Bind actual device, runtime, provider digest and native client code to an
immutable OCI image and stopped scope. The optional native client is explicitly
selected through the upstream event interface, with no automatic replacement
of other coding backends.

Persist private attestation for active-run restart. Recheck boot/image/profile/
code, stopped probe and live identity without another GPU/model inference.
A changed profile requires quiescent indexed ownership, including legacy
records. Test the HTTP, OCI and native protocol seams already owned by R8.
Keep Oracle thresholds and separate availability, coding quality and full
optimization acceptance.

## Native artifact and budget boundaries (2026-10-12 continuation)

Make the actual provider context and output budgets explicit, bounded and part
of the effective launch/session profile. A default omitted from the attestation
environment remains binding. Reject provider-declared output exhaustion before
returning text or tools; preserve the failed response instead of repairing it.

Add one native file tool for real model bytes, using the existing contained
workspace. No-follow directory traversal, regular targets, bounded content,
atomic publication and mandatory FD cleanup apply. The Codex event interface
reports actual file changes with IDs distinct from command/message items.
The upstream file requirement, protected reference, Oracle and delivery gates
remain authoritative; a written harness can still fail its input contract.

Keep Windows checkpoint publication atomic under readers. Retry only the
observed sharing/access denial errors, at most five attempts with bounded
backoff; persistent errors preserve previous bytes and propagate normally.

## Harness acceptance continuation (2026-10-12)

Pass the caller's remaining native HTTP operation budget to the actual opener;
the outer request process still enforces the absolute deadline. A separate
180-second socket/header ceiling incorrectly rejected slow real model responses
before their caller budget expired. Keep DNS, headers and body under the same
deadline and preserve exhaustion refusal.

Improve model instructions for existing reference imports, the documented
callable and actual harness execution. Instructions remain heuristics. A
reference self-test alone cannot detect a harness bypassing the public wrapper;
use an interface-compatible wrong wrapper retaining the original raw kernel.
Preserve genuine failed files and reviews without rewriting them into passes.

The real UC workflow also exposed missing live-form choice validation:
max_iterations=1 was refused by the pinned form before task creation. Validate
single/multiple option labels before reservation, intent and POST. Do not coerce
answers or alter the pinned form. Existing uncertain records remain isolated;
a local validation repair cannot retroactively prove their remote outcome.

The fixed review parser also misclassified an actual heading-only FAIL by
counting passed substrings in its body. Add a native completion boundary for
the two existing review files: one leading canonical verdict, bounded no-follow
regular-file reads and refusal before final completion. Deliver a valid FAIL
unchanged; never manufacture PASS or treat format validation as correctness.
This additive guard leaves the pinned source untouched and requires a new
attested native image/profile. Earlier image6 attempts remain earlier evidence.

## Submission identity recovery continuation (2026-10-13)

The deployed contained service returned HTTP 404 for the identity route already
used by UC's operator `attach` command. Implement that existing seam with an
additive `job_identity` capability. Bind the UC operation label and canonical
task request SHA into the private launch intent and OCI labels before create;
read only that binding and verify the immutable observed container on identity
requests. Preserve old jobs without bindings as unavailable; never derive
trusted identity later from job-writable metadata. Identity observation and
attachment retain reservations until separate stopped-scope confirmation.

Continue genuine optimization in the existing image7 deployment with a fresh
worktree/state and a valid inner iteration choice, using a 3600s execution
budget. Protected Oracle policy stays unchanged. Preserve original failed
model output and all-writer evidence; successful transport/harness self-tests
alone cannot close R8 or its user-deferred cross-host criterion.

The image7 trial also reproduced a completion-boundary bug: an unchanged
malformed review from an earlier phase blocked a generator that changed only
its harness. Snapshot bounded regular review bytes at invocation entry only
for the exact pinned correctness/performance generator headers. On completion,
preserve only identical pre-existing reviews; validate all new/changed ones.
Reviewers and generic prompts stay strict. Preserve rejected feedback rather
than rewriting it. Build image8 with this native change and fresh attestation;
the stopped image7 attempt remains a failed attempt, not accepted optimization.

## Genuine Gemma verification continuation (2026-10-13)

Reuse merged image8 without source changes and explicitly select the installed
Gemma model in separate roots for 4096/8192 output budgets. Keep the protected
reference, baseline sampling, Oracle thresholds and upstream pin unchanged.
Record actual file and command events separately from model completion prose.
On an identified blocked trial, obtain the service's stopped-writer evidence,
let UC settle rollback and reservations, and independently read Git/SQLite.
Observe provider unload separately: an Ollama unload acknowledgement may arrive
while an in-flight buffered request still keeps the model loaded. Preserve all
original failed outputs, previous manifests/audits and the unknown quarantine.
These failed trials extend runtime evidence; they do not complete R8.

## Explicit native thinking mode (2026-10-14)

The installed 4B thinking-only weights reproduced nonce failure when the
native client fixed think:false. Conditional and unconditional empty-thinking
prefills did not repair it. The provider's actual think:true API separated
reasoning from final nonce content and returned formal file-tool calls.
Add a strict, explicit UC_OLLAMA_THINK profile option, default false. Bind
the effective mode to launch equality and sessions; bind the environment to
attestation. Legacy sessions without think remain false-only. Keep native
identity wire shape, response bytes, deadlines and Oracle policy unchanged.
Use a new immutable image and fresh control root. The JSON tool template is
provider configuration using Ollama's formal legacy tool parser, not a UC
prose-to-command conversion. Preserve all failed aliases/probes and old audits.

The first full thinking-mode trial returned four code-only final messages and
no required artifact; the pin repeatedly returned logic_fail. Stop that exact
trial with cgroup evidence. For the two exact pinned generator headers, read
the requested harness through the existing no-follow bounded artifact reader.
Missing/empty bytes prompt at most two real model feedback requests within the
existing turn/deadline budget, then refuse completion. Persist feedback and
original responses; reject links/special/oversized artifacts immediately.
Generic text completion and rejected-review snapshots retain their contracts.
Actual artifact existence is distinct from its correctness and Oracle verdict.
