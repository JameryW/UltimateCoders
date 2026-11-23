# V23 Architecture Upgrade Map

This map records the v23 upgrades requested in the architecture ledger
(seventeenth period, items #97–#102). The additions are opt-in libraries:
four Rust modules in `uc-engine`, two Python components, and no new
service, database migration, scheduler, container runtime, or live route.
Every feature flag defaults off.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Sandbox residency virtualisation | `crates/uc-engine/src/tenon_residency.rs` | n/a (library) | sandbox scheduling; no engine caller yet | module only |
| MCP germline isolation | `crates/uc-engine/src/germline_mcp.rs` | n/a (library) | MCP execution boundary; no caller yet | module only |
| Causal privilege minimisation | `crates/uc-engine/src/prism_agentcap.rs` | n/a (library) | tool authorisation; no caller yet | module only |
| Shadow MCP routing | `python/ultimate_coders/agent/shadow_mcp_router.py` | `UC_SHADOW_MCP_ROUTER` | tool dispatch; no worker caller yet | module only |
| Skill intent auditing | `python/ultimate_coders/search/skill_intent_scanner.py` | `UC_SKILL_INTENT_SCANNER` | skill loading and provenance; no caller yet | module only |
| Metal command scheduling | `crates/uc-engine/src/metal_scheduler.rs` | n/a (library) | cross-node execution; no engine caller yet | module only |

## Sandbox residency virtualisation (#97)

`tenon_residency.rs` models the two-timescale TenonAgent admission policy.
`TenonResidency::plan` keeps `Active` and `ToolCall` sessions materialised,
de-materialises idle sessions, and rejects the newest active overflow when
the materialised memory exceeds the budget multiplied by the configured
oversubscription. The plan reports admitted/rejected sessions, total
materialised MB, and the resulting oversubscription. The module is a pure
planner; it does not create or destroy real sandboxes.

## MCP germline isolation (#98)

`germline_mcp.rs` represents a pool of data-free germline processes.
`GermlinePool::execute` forks a transient copy for a `ToolInvocation`,
filters the payload to the labels authorised for that invocation, records
denied keys, and returns an isolated execution plan. `release` reclaims the
fork. A pool at capacity returns `PoolExhausted`; a fork never reuses
another fork's id. The module does not spawn operating-system processes.

## Causal privilege minimisation (#99)

`prism_agentcap.rs` contains `PrismMinimizer`, which keeps required tools
and tools whose counterfactual necessity reaches the threshold, reports the
compression ratio and the maximum blast radius of the retained set, and
drops the rest. `CapabilityIssuer` creates child tokens whose budget and TTL
can only shrink relative to the parent, while `AgentCapVerifier` checks
revocation, expiry, session, tool, and requested budget before use. The
module is a policy model, not an authorisation server.

## Shadow MCP routing (#100)

`shadow_mcp_router.py` scores `ToolCandidate`s with the SODS combination of
task preservation, operation pressure, and route geometry drift. Shadowed
or disallowed candidates are blocked. Security-critical and audit-required
tools are mandatory: if one is shadowed, the whole route is rejected rather
than silently skipped. `PollingGuard` applies exponential backoff to
repeated state digests and stops at a monotone completion bound.

## Skill intent auditing (#101)

`skill_intent_scanner.py` combines static regular-expression rules,
sandbox dynamic signals, and optional LLM review into a deterministic risk
score. `SkillIntentScanner::scan` reports findings and a malicious flag at
the configured threshold. `TaskletLedger` maps each generated line back to
the planning and build prompts that produced it and exports a line-level
ADR. The module does not execute skills or call a model.

## Metal command scheduling (#102)

`metal_scheduler.rs` schedules parallel and serial `MetalCommand`s across
Apple Silicon nodes, reporting assignments, makespan, and throughput.
`CrossProviderReview` requires at least two distinct providers and treats
coverage of injected defects as the provenance signal. `CollusionMonitor`
records approval decisions and reports a lock-step approval score based on
repeated evidence digests. The module is an in-memory scheduler; it does not
connect to Mac nodes or run commands.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v23 -q --no-cov
# 20 passed, 4362 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/agent/shadow_mcp_router.py \
  python/ultimate_coders/search/skill_intent_scanner.py \
  tests/python/test_v23_*.py
# All checks passed

cd packages/uc-orchestrator && bun test src/rl src/skills src/harvest src/harness src/telemetry
# 192 pass, 0 fail

cargo test -p uc-engine --lib
# 840 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory. `cargo fmt -p uc-engine -- --check`
is clean.

## Non-goals

- `tenon_residency` does not materialise or destroy real sandboxes.
- `germline_mcp` does not spawn processes; callers supply the execution
  environment.
- `prism_agentcap` is not an authorisation server and does not issue real
  tokens.
- `shadow_mcp_router` does not call MCP servers; it only decides routing.
- `skill_intent_scanner` does not execute third-party skills.
- `metal_scheduler` does not connect to Apple Silicon nodes.
