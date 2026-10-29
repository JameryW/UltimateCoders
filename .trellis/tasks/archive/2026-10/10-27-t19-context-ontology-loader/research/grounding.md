# T19 grounding — CONTEXT ontology loader

Authority inputs: [D9](../../../../docs/architecture/agent-trust-eval/issues/D9-project-ontology-mcp.md), [ticket 19](../../../../docs/architecture/agent-trust-eval/tickets/19-context-ontology-loader.md), [spec-p3](../../../../docs/architecture/agent-trust-eval/spec-p3.md), [docs/agents/domain.md](../../../../docs/agents/domain.md), [CONTEXT-FORMAT](../../../../.agents/skills/domain-modeling/CONTEXT-FORMAT.md), Hy-Memory / engine MCP peers surveyed 2026-10-27 on `cursor/t19-context-ontology-loader-7c92`.

Thoroughness: medium. Goal: pin authority, flag/loader shape, minimal seed vocabulary, and a T19-only API that T20 can wrap without inventing a second format.

---

## 1. `docs/agents/domain.md` expectations for CONTEXT.md

From `docs/agents/domain.md`:

- UltimateCoders is a **single-context** repo.
- When present, agents should read root `CONTEXT.md` for **canonical domain vocabulary**.
- `CONTEXT.md` and `docs/adr/` are created **lazily** (domain-modeling / grill-with-docs): write only when a term is resolved or a hard-to-reverse trade-off is decided.
- Keep implementation detail in specs/tasks — **not** in the glossary.

Matt skill format (`.agents/skills/domain-modeling/CONTEXT-FORMAT.md`):

```md
# {Context Name}

{1–2 sentence what/why}

## Language

**Term**:
{one or two sentences — what it IS}
_Avoid_: synonym1, synonym2
```

Rules that bind T19 parsing:

- Opinionated: pick one term; list rejects under `_Avoid_`.
- Tight definitions (1–2 sentences).
- Project-specific nouns only (no general programming concepts).
- Single-context → **one root `CONTEXT.md`** (no `CONTEXT-MAP.md` for MVP).

**Repo fact (2026-10-27):** root `CONTEXT.md` is still **absent**. Spec-ref guards already exempt dangling mentions in `domain.md` / D9 / map until a P3 ticket creates the file. Seeding a minimal file in T19 is the intended graduation of that lazy path — humans remain the only writers.

---

## 2. Existing MCP / flag / loader patterns to mirror

### 2.1 Surfaces that are *not* ontology

| Surface | Role | Use as glossary? |
|---------|------|------------------|
| `engine_mcp.py` | stdio MCP: search, layered memory, optional Hy-Memory + DeepSeek web | No — memory/search |
| `hy_memory.py` | Cognitive memory backend + optional auto-context | No — trajectory facts |
| Codegraph | Symbol/call graph | No — code graph |
| `docs/adr/` | Surprising trade-offs | Not term store |

### 2.2 Flag + soft-empty peer: Hy-Memory auto-context (T12)

`python/ultimate_coders/agent/hy_memory.py` is the closest product pattern:

- Env constant: `HY_MEMORY_AUTO_CONTEXT_ENV = "UC_HY_MEMORY_AUTO_CONTEXT"`.
- Truthy set: `frozenset({"1", "true", "yes", "on"})` (case-normalized strip).
- Gate: `hy_memory_auto_context_enabled()` → **default off** when unset/empty.
- Soft skip: `build_hy_memory_context_block(...)` returns `None` on flag-off path consumers, missing package, construct failure, or empty recall — **never raises**.
- MCP registration is separate: `engine_mcp` hides Hy-Memory tools when `hy_memory_available()` is false; it does **not** invent a second authority.

Landlock / NetworkMode use the same `_TRUTHY_ENV` convention. T19 should reuse that parser shape for `UC_ONTOLOGY_MCP`.

### 2.3 Engine MCP registration (deferred to T20)

`engine_mcp.py` builds tool lists conditionally (`_memory_tools`, `_deepseek_web_tools`) and advertises only when prerequisites hold. T20 will likely add `list`/`lookup` the same way **behind** `UC_ONTOLOGY_MCP`. T19 must **not** register tools — only export a stable in-process API that those tools will call.

### 2.4 Implication for T19

- New module peer to `hy_memory.py` (recommended path: `python/ultimate_coders/agent/ontology.py`).
- Flag default off; missing / unreadable CONTEXT → empty structure, no exception.
- No imports from codegraph clients or Hy-Memory for glossary content.
- No write / remember / delete / mutate APIs.

---

## 3. Sensible minimal CONTEXT.md seed (few terms)

D9 fog grounding listed ~15–25 candidates. For T19 “few glossary terms,” ship a **closed starter** of orchestration nouns agents already collide on — not the full trust-eval catalog (those can be added by humans later).

Recommended seed (~7 terms), Matt format under `## Language`:

| Term | Definition intent (draft) | `_Avoid_` hints |
|------|---------------------------|-----------------|
| **Gateway** | Independently deployed Orchestrator-side Rust gRPC control plane (Engine/Task/Dashboard/Worker services + WorkerRegistry). | Orchestrator server, control plane (as synonym dump) |
| **Worker** | Independently deployed Python execution unit that registers over gRPC and runs subtasks via NATS. | Agent host, sandbox host (alone) |
| **Task** | User intent + acceptance criteria owned as a Gateway task record. | Job, ticket (in runtime sense) |
| **Subtask** | Independently schedulable work unit under a Task (agent-os Step correspondence). | Step (as synonym; prefer Subtask in UC prose) |
| **Attempt** | One try at executing a Subtask; may fail and increment without minting a new Subtask identity. | Retry (alone), Effect |
| **ExecutionEnvelope** | Dispatch identity carried on `uc.subtask.execute` (`graph_id` / `node_id` / `attempt_id` / `idempotency_key` / `worker_epoch` / `contract_version`). | Payload, message body |
| **CONTRACT_VERSION** | Lockstep execution-contract version string shared by Gateway and workers; mismatch refuses register/heartbeat or blocks dispatch. | Schema version (alone) |

Optional later (out of T19 seed unless preferred): `MergeArbiter`, `eval_role`, `NetworkMode`, `Hy-Memory` — trust/eval and git arbitration terms are real but expand the MVP glossary past “few.”

Authority path field on parsed terms: default `CONTEXT.md` (repo-relative); optional per-term override not required for MVP seed.

---

## 4. Recommended T19 API (Approach A input)

**Approach A (CONTEXT.md authority + in-process loader; MCP deferred):** matches D9 / ticket 19 / domain.md.

```text
python/ultimate_coders/agent/ontology.py
├── ONTOLOGY_MCP_ENV = "UC_ONTOLOGY_MCP"
├── ontology_mcp_enabled() -> bool          # same truthy parser; default off
├── @dataclass OntologyTerm
│     name: str
│     definition: str
│     avoid: tuple[str, ...] = ()
│     authority_path: str = "CONTEXT.md"
├── load_ontology(*, path=None, enabled=None) -> list[OntologyTerm]
│     # flag off OR missing/unreadable file → []
│     # never raises for those soft cases
├── list_terms(...) -> list[OntologyTerm]   # thin alias / filtered list over load
└── lookup_term(name, ...) -> OntologyTerm | None
```

Parsing notes (MVP):

- Read UTF-8 Markdown from repo-root `CONTEXT.md` (overridable `path` for tests).
- Extract `**Term**:` blocks under Language (ignore prose outside term blocks).
- Case-insensitive lookup; preserve canonical casing from the file.
- Malformed sections: skip term or soft-empty that entry — do not crash process.
- **No** `write_*`, `remember_*`, `delete_*`, `save_*`.
- **No** MCP `Tool(` registration in this module or in `engine_mcp` under T19.

Tests (no live MCP server): flag off → `[]` / `None`; flag on + missing file → `[]` / `None`; flag on + seeded fixture → list/lookup hit; assert module has no write API surface.

---

## 5. Feasible approaches (for PRD)

**A. CONTEXT.md + `ontology.py` loader (Recommended)**  
Human-owned root glossary; flag-gated soft-empty loader; T20 registers MCP later. Pros: matches domain.md + D9; smallest ship; mirrors Hy-Memory flag culture. Cons: must create currently-absent `CONTEXT.md`; parser must tolerate Markdown drift.

**B. Docs-path glossary (`docs/agents/glossary.md`) + loader**  
Same loader, different authority path. Pros: keeps root clean. Cons: diverges from Matt default and domain.md pointer.

**C. Generate glossary from code enums / protos**  
Always “in sync” with typed enums. Pros: low drift vs FailureKind etc. Cons: wrong layer; misses architectural nouns; contradicts D9 human authority.

---

## 6. Explicit non-goals (T19)

- MCP tool registration (`list_domain_terms` / `lookup_domain_term`) — **T20**.
- Write-back / Evo / LLM-inferred ontology.
- Codegraph or Hy-Memory as glossary source.
- Multi-context `CONTEXT-MAP.md`.
- Auto-inject glossary into every worker prompt.

---

## 7. Key file references

- `docs/agents/domain.md`
- `.agents/skills/domain-modeling/CONTEXT-FORMAT.md`
- `docs/architecture/agent-trust-eval/issues/D9-project-ontology-mcp.md`
- `docs/architecture/agent-trust-eval/tickets/19-context-ontology-loader.md`
- `docs/architecture/agent-trust-eval/tickets/20-ontology-mcp-readonly.md`
- `docs/architecture/agent-trust-eval/spec-p3.md`
- `python/ultimate_coders/agent/hy_memory.py` (flag + soft-skip peer)
- `python/ultimate_coders/agent/engine_mcp.py` (conditional tool advertising; T20 seam)
- `docs/agent-os-architecture.md` §3 (entity vocabulary for seed)
