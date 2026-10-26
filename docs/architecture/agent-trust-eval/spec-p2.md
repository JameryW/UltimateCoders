# Agent Trust / Eval — P2 Spec

Status: **ready-for-agent** (planning artifact; no product code in this change)
Authority: [D7](issues/D7-stall-replan-ownership.md), [D8](issues/D8-sandbox-isolation-target.md); inherits P0 completion conditions (D3) and P1 product flags
Map: [map.md](map.md)
Depends on: P0–P1 tickets 01–12 implemented (completion conditions, dual-gate, etc. already in tree)

## Problem Statement

P0–P1 made eval identity, dual correctness, completion, approx prune, typed review, and Hy-Memory wiring enforceable behind flags. Two P2 gaps remain on the decided map: (1) progress stall still only stops retries — there is no same-contract one-shot Agent redispatch, so “换一个 Agent 重派一次” is aspirational; (2) `NetworkMode` is config theatre — `none` / `restricted` / `full` do not change agent-subprocess networking, so operators can believe they are offline when they are not. True replan (new decomposition) and Landlock FS stay deferred per D7/D8.

## Solution

Ship P2 as two coordinated product seams, all behind feature flags that default off:

1. **Same-contract stall redispatch (D7 MVP).** Observe progress-stop / `loop_detected` as a first-class signal. When enforce is on, orchestrator (or equivalent dispatch owner) may redispatch the same contract digest once with an alternate registered Agent / worker binding. Second failure escalates to human. Do not invent replan inside the progress detector. True replan (new subgraph / new contract version) is a later ticket after completion-conditions enforce is product-stable.

2. **Real NetworkMode on the Agent child (D8 step 1).** Enforce `None` / `Restricted` / `Full` at agent-subprocess spawn (netns / seccomp-bpf / equivalent), not on the whole worker (NATS/Gateway stay up). Default-toward-offline and LLM egress allowlist land behind flags once enforcement exists. Landlock FS is a later ticket on the same map.

## User Stories

1. As an operator, I want a stalled subtask to try one alternate Agent under the same contract, so that flaky agent-specific loops get one recovery chance without rewriting the goal.
2. As a safety reviewer, I want a second stall failure to escalate to human, so that infinite agent-swapping cannot burn quota.
3. As a progress-detector maintainer, I want redispatch owned outside the detector module, so that “no replan here” stays true.
4. As a contract owner, I want same-contract redispatch to preserve digest identity, so that it is not confused with `contract_changed` replan.
5. As a sandbox operator, I want `NetworkMode.none` to actually block agent-child egress, so that configured offline is not fake safety.
6. As a platform engineer, I want Restricted mode to allow only an explicit LLM/API host allowlist on the child, so that coding agents can call models without opening the world.
7. As a worker SRE, I want NATS/Gateway connectivity preserved on the worker process, so that heartbeats and dispatch continue under child isolation.
8. As a feature-flag owner, I want all new P2 enforcement off by default, so that existing deployments stay compatible until opted in.
9. As an implementer of ticket 14, I want ticket 13’s observable stall surface, so that redispatch has a stable signal.
10. As an implementer of ticket 16, I want ticket 15’s real NetworkMode enforcement, so that default-offline is not another config lie.
11. As a wayfinder reader, I want P2 tickets linked from the map as ready-for-agent, so that agents know the grabable frontier after P1.
12. As a grilling follow-up owner, I want project ontology MCP and README numeric CI left fog (not in this spec), so that P2 stays shippable on D7–D8 only.
13. As a corpus guardian, I want planning docs to avoid inventing untracked path-shaped backticks, so that the spec-ref mention census stays clean.

## Implementation Decisions

- **Orchestrator owns one-shot redispatch.** Progress detector remains stop/retry evidence only. Plug after `loop_detected` / terminal progress-stop, not inside progress evaluation.
- **Same digest only.** Redispatch must not bump contract version or rewrite goal text; that path is true replan (deferred).
- **One swap.** Quotas: exactly one alternate-agent attempt per stall episode; then human.
- **Child-only network cut.** Enforcement attaches to agent subprocess spawn; worker process keeps control-plane egress.
- **Fail closed on fake isolation.** If enforce is on and the platform cannot apply the requested mode, refuse start rather than silently running Full.
- **Landlock deferred.** Network tickets must not wait on FS Landlock.
- **Flags default off.** Mirror P0/P1 `UC_*_ENFORCE` style.

## Testing Decisions

1. **Seam G — stall redispatch.**  
   - Flag off: progress-stop still stops; no auto agent swap.  
   - Flag on: one alternate-agent redispatch under unchanged digest; second failure escalates.  
   - Progress module still has no replan API.

2. **Seam H — NetworkMode.**  
   - Flag off: spawn behaviour unchanged (compatible Full).  
   - Flag on + None: child cannot reach external hosts (testable with a probe command).  
   - Restricted: allowlist hosts reachable; others blocked.  
   - Worker heartbeat path unaffected in tests that only isolate the child.

Prefer one vertical demo per ticket plus focused unit tests on the two seams.

## Out of Scope

- True replan / new decomposition (D7 deferred follow-on).
- Landlock FS (D8 later ticket).
- Project ontology via MCP; README / docs numeric recomputation CI (still fog).
- Publishing GitHub wayfinder issues without explicit authorization.
- Cutting network on the entire worker container.

## Further Notes

- Feature flags for P2 enforcement stay **off by default**.
- Decision authority remains D7–D8; this spec and tickets 13–18 are the implementation backlog linked from [map.md](map.md).
- Vertical slices: T13 none → T14←T13; T15 none → T16←T15; T17←T16 (Landlock later). Frontier after publish: T13, T15.
