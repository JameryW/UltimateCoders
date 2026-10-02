# MetaInfer reliability repair

Source ticket: `.scratch/metainfer-reliability/issues/01-runtime-reliability.md`.
Baseline: `7d6bfbfa`. User approved all architecture review findings for repair.

Implement the ticket's acceptance criteria inline in this context. The earlier session fallback
points to already delivered coding-adapter work; this is the current repair task.

## Design decisions

- Keep the external MetaInfer protocol. Persist submission intent before network side effects;
  unknown submission outcome must never trigger another blind creation.
- Production runtime records use the existing PostgreSQL deployment. SQLite is a local-only
  fallback. Database failures are explicit when a database URL is configured.
- Keep execution envelope compatibility by carrying UC identity through existing agent configuration.
- Workspace leases and experiment transaction checkpoints live outside candidate-editable code.
- Accepted code, committed code and merged delivery are distinct states.
- Result outbox retransmission is independent of execution; use idempotent message/attempt identity.
- Capability probing is asynchronous and cached; an unreachable URL is not an advertised capability.
- Existing Dashboard/API and event channels expose artifacts and experiment phases; no GPU libraries
  enter the UC Worker image.

## Implementation order

1. Reproduce and repair exclusive workspace allocation and accepted patch delivery.
2. Add shared runtime persistence, remote operation tracking, uncertainty quarantine and resumable experiments.
3. Persist and resend terminal outcomes, then implement live capabilities and local benchmark routing.
4. Add manifests/artifact queries, repeated statistical acceptance and phase events.
5. Validate through the ticket's public seams, review, update specs/docs, and commit.

## Required sources

- `.trellis/spec/backend/inference-infra-spec.md`
- `.trellis/spec/backend/nats-bridge-spec.md`
- `.trellis/spec/backend/agent-capability-spec.md`
- `.trellis/spec/backend/database-guidelines.md`
- `.trellis/spec/backend/quality-guidelines.md`
- `.trellis/spec/guides/cross-layer-thinking-guide.md`
- `.trellis/spec/guides/cross-platform-deployment-thinking-guide.md`
