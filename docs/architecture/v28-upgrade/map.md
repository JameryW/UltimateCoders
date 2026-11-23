# V28 Architecture Upgrade Map

This map records the v28 upgrades requested in the architecture ledger
(twenty-second period, items #127–#132). The additions are opt-in libraries:
two Rust modules under existing subsystems, four Python components, and no new
service, database migration, scheduler, container runtime, or live route.
Every feature flag defaults off.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Instruction-level skill evolution | `python/ultimate_coders/knowledge/skill_plumber.py` | `UC_SKILL_PLUMBER` | skill curriculum; no caller yet | module only |
| Prefix-cache metastability budget | `crates/uc-engine/src/scheduler/kv_metastability_budgeter.rs` | n/a (library) | scheduler admission; no engine caller yet | module only |
| Scoped memory and risk fence | `python/ultimate_coders/memory/mixua_scope_guard.py` | `UC_MIXUA_SCOPE_GUARD` | memory recall; no caller yet | module only |
| Hard/soft rule audit and propagation | `crates/uc-engine/src/governance/dual_rule_auditor.rs` | n/a (library) | governance audit; no engine caller yet | module only |
| Serving and compression benchmark | `python/ultimate_coders/inference/server_compression_bench.py` | `UC_SERVER_COMPRESSION_BENCH` | benchmark runner; no caller yet | module only |
| Certificate and formal-proof audit | `python/ultimate_coders/acceptance/certclaim_audit.py` | `UC_CERTCLAIM_AUDIT` | acceptance review; no caller yet | module only |

## Instruction-level skill evolution (#127)

`skill_plumber.py` separates "the instruction is wrong" from "the executor did
not follow it". `InstructionStatus` records follow / not-follow / not-covered
for each instruction in a `SkillTrace`-style observation set. `CoverageMatrix`
reports per-instruction coverage and follow rate, `SkillPlumber.questions`
turns the matrix into bounded `RevisionQuestion`s, and `validate` only accepts a
revision that resolves the question it was generated for. `MaintainerAgent`
distills validated questions into a deduplicating `PolicyBank`. The module does
not rewrite a skill with a model; callers provide the proposed revisions.

## Prefix-cache metastability budget (#128)

`kv_metastability_budgeter.rs` treats resident KV demand as a first-class
admission quantity. `SessionDemand` carries resident megabytes, a priority, and
a shared-prefix size. `KvMetastabilityBudgeter.plan` keeps the resident total
under `capacity * safety_ratio`; when over the limit it suspends accepted
sessions by ascending priority, then ascending shared-prefix score, then
descending demand. The incoming session is never suspended and is rejected only
when even suspending every existing session cannot make room. Each plan reports
admitted, suspended, rejected, resident usage, and saturation. The module does
not talk to vLLM or SGLang; it is an admission policy over caller-supplied
demand.

## Scoped memory and risk fence (#129)

`mixua_scope_guard.py` classifies memories into four scopes: user-agent pair,
user, agent, and global. `MemoryRecord` also carries a `RiskTier`, and
`TaskContext` declares the current user, agent, risk tier, and allowed scopes.
`MixUaScopeGuard.evaluate` blocks scope mismatch, identity mismatch, and a
memory whose risk tier is *lower* than the task's risk tier, which is the
Memory Trap shortcut path. Higher-risk memories remain eligible for lower-risk
tasks. `retain` returns only the surviving records and `ScopeReport` reports an
isolation ratio. The module is pure bookkeeping and stores no memory itself.

## Hard/soft rule audit and propagation (#130)

`dual_rule_auditor.rs` combines deterministic hard rules with a weighted soft
screen. `HardRule` blocks a forbidden action prefix or a protected target
prefix. `SoftRule` scores suspicious terms; the soft reasons are emitted only
when the accumulated score reaches `soft_threshold`, so a low-score hint does
not deny an otherwise clean proposal. `PropagationToken` carries a budget and a
hash-linked hop chain; `sign_hop` and `verify` use a keyed BLAKE3 MAC rather
than Ed25519 because the engine dependency set has no signing key
infrastructure. The audit trail is a bounded, hash-linked sequence and
`verify_chain` detects tampering. This is a deterministic policy audit, not a
cryptographic identity system.

## Serving and compression benchmark (#131)

`server_compression_bench.py` scores serving variants by qualified tokens per
GPU dollar while enforcing a p95 latency budget and independent verification.
`CompressionBench` keeps local perplexity ranking separate from end-to-end
downstream accuracy, reports rank reversals, and passes a candidate only when
it is independently verified, its perplexity increase stays within the limit,
and its downstream accuracy stays above the floor. The module does not launch a
server; callers supply measured variants.

## Certificate and formal-proof audit (#132)

`certclaim_audit.py` checks a provider/model/deployment claim against an
authorized deployment set and a set of calibrated channels. `CertClaimAuditor`
combines mean channel drift with a sampling term, rejects unauthorized
deployments, too few channels, drifted channels, and an exceeded error bound,
and returns a certificate with the reasons. `CrossAgentFormalAuditor` requires
each proof to be verified, semantically matched, re-extracted as matched, and
free of direct calls to the specification predicate; it reports consistency and
rejected specs. `CriteriaExposureGuard` rejects hidden criteria that were
exposed to the agent. The module does not invoke Dafny; callers provide proof
audit records.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v28 -q --no-cov
# 42 passed, 4504 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/knowledge/skill_plumber.py \
  python/ultimate_coders/memory/mixua_scope_guard.py \
  python/ultimate_coders/inference/server_compression_bench.py \
  python/ultimate_coders/acceptance/certclaim_audit.py \
  tests/python/test_v28_*.py
# All checks passed

cargo test -p uc-engine --lib
# 930 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory. `cargo fmt -p uc-engine -- --check` is
clean. The existing TypeScript suite is unchanged at 247 pass; V28 adds no TS
module.

## Non-goals

- `skill_plumber` does not rewrite skills with a model; it validates revisions.
- `kv_metastability_budgeter` does not own a KV cache or call an inference server.
- `mixua_scope_guard` does not persist memories or alter recall scores.
- `dual_rule_auditor` does not provide Ed25519 identity or a distributed ledger.
- `server_compression_bench` does not launch servers or run compression kernels.
- `certclaim_audit` does not run Dafny or contact a model provider.
