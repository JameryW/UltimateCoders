# UltimateCoders

Single-context domain vocabulary for UltimateCoders: orchestration nouns agents
must use consistently. Humans own this file; agents must not rewrite it.

## Language

**Gateway**:
Independently deployed Orchestrator-side Rust gRPC control plane hosting
Engine, Task, Dashboard, and Worker services plus WorkerRegistry.
_Avoid_: Orchestrator server, control plane

**Worker**:
Independently deployed Python execution unit that registers over gRPC and runs
subtasks via NATS.
_Avoid_: Agent host, sandbox host

**Task**:
User intent plus acceptance criteria owned as a Gateway task record.
_Avoid_: Job, ticket

**Subtask**:
Independently schedulable work unit under a Task (agent-os Step correspondence).
_Avoid_: Step

**Attempt**:
One try at executing a Subtask; may fail and increment without minting a new
Subtask identity.
_Avoid_: Retry, Effect

**ExecutionEnvelope**:
Dispatch identity carried on `uc.subtask.execute` (`graph_id` / `node_id` /
`attempt_id` / `idempotency_key` / `worker_epoch` / `contract_version`).
_Avoid_: Payload, message body

**CONTRACT_VERSION**:
Lockstep execution-contract version string shared by Gateway and workers;
mismatch refuses register/heartbeat or blocks dispatch.
_Avoid_: Schema version
