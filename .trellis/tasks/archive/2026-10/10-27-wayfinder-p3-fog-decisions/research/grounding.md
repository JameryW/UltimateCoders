# P3 fog grounding — D9 ontology MCP + D10 README numeric CI

Authority inputs: [map.md](../../../../docs/architecture/agent-trust-eval/map.md) «Not yet specified», [assessment](../../../../docs/architecture/agent-trust-eval-assessment.md) §五/§六 fog rows, D7/D8 resolution shape, existing MCP / docs / CI guards surveyed 2026-10-27 on `cursor/agent-trust-eval-p3-decisions-7c92`.

Thoroughness: medium. Goal: graduate two fog items into decision tickets with **Proposed** resolutions matching D7/D8 (MVP small, flags off by default, hard parts deferred).

---

## 1. What the map calls fog

From `docs/architecture/agent-trust-eval/map.md` «Not yet specified»:

1. **项目本体（EvoOntology 风格）经 MCP 可查的最小词表与权威源**
2. **README / 文档中的数字由哪条脚本复算、CI 失败策略（漂移告警 vs hard fail）**

Destination text also lists both under P2 destination; `spec-p2.md` left them **explicitly out of P2 MVP** so D7–D8 could ship first. P0–P2 implementation backlog is closed (01–17); these two remain the only non-ticketed fog items (aside from deferred true-replan T18 and labeling-pipeline fog under D5).

D7/D8 style to match:

- One crisp **Question**
- Short **Context** tied to repo facts
- **Resolution** = MVP slice + deferred hard part + ownership / flag posture

---

## 2. Fog item A — Project ontology via MCP (→ proposed D9)

### 2.1 Existing surfaces (what is *not* an ontology)

| Surface | Role today | Ontology? |
|---------|------------|-----------|
| `docs/agents/domain.md` | Points at lazy `CONTEXT.md` + `docs/adr/`; says single-context repo | Pointer only — **no glossary body** |
| `CONTEXT.md` / `docs/adr/` | Domain-modeling skill expects them | **Absent** (confirmed 2026-10-27) |
| `docs/agent-os-architecture.md` | Entity model (Task/Run/Step/Attempt/Effect), authority table, F-series gaps | Closest **human** domain write-up; not MCP-served |
| `AGENTS.md` / CLAUDE.md | Architecture + contract handshake prose | Operator docs, not a term store |
| `python/.../agent/engine_mcp.py` | MCP: `search_code`, `read_memory`/`write_memory`, optional Hy-Memory + DeepSeek web | Memory/search — **no term lookup** |
| Codegraph (`.codegraph/`, `CodegraphClient`, worker tools / `mcp__codegraph__*`) | Symbol/call/impact graph over source | **Code** graph, not project concept ontology; pre-processing path removed (spec banner) |
| `routing.py` `FailureKind` / `RiskLevel` | Enum vocab for failure routing (D5 aligns reviewers here) | Typed enums ≠ MCP ontology |
| Trust-eval decided terms | `eval_role`, report-only uplift, `completion_conditions`, NetworkMode, stall redispatch | Scattered in specs/tickets; not a single queryable store |

**EvoOntology-style** in map prose means: agents can **query project concepts** (entities, relations, authority) via MCP — not “index every symbol” (that is codegraph) and not “replace Hy-Memory facts.”

No in-repo package, proto, or ticket implements project ontology MCP. No `EvoOntology` dependency or vendored code found.

### 2.2 Minimal vocabulary that could ship (MVP candidate)

Ship a **small closed glossary** (order of ~15–25 terms), not a living knowledge base. Candidate seed from docs that already exist:

**Runtime / orchestration** (from agent-os §2–3): Task, Subtask/Step, Attempt, Effect, Run, contract / `CONTRACT_VERSION`, Worker vs Gateway vs Orchestrator.

**Trust / eval** (from D1–D8): `eval_role` ∈ {search, select, report}, public uplift (report-only), Oracle accept vs publish, `completion_conditions`, approx prune (not uplift), typed reviewer / FailureKind alignment, Hy-Memory supersede-not-delete, stall same-contract redispatch, NetworkMode {none,restricted,full}.

**Non-goals for MVP vocab:** full Durable Runtime P2 optimizer ratios, MetaInfer plugin taxonomy, every env var, codegraph symbol kinds.

Format: Matt-style `CONTEXT.md` glossary (term → one-paragraph definition; avoid synonyms the glossary rejects). Optional thin `relations:` (e.g. Attempt belongs-to Subtask) — keep optional so MVP stays editable Markdown.

### 2.3 Authority source options

| Option | How it works | Pros | Cons |
|--------|--------------|------|------|
| **A. `CONTEXT.md` (recommended)** | Human-edited glossary at repo root; MCP reads/parses it | Matches `docs/agents/domain.md` + domain-modeling skill; lazy-create already prescribed; single context | Must create file that does not exist yet; agents must not invent terms silently |
| **B. `docs/agents/glossary.md` (or split under docs/)** | Same as A but under docs/ | Keeps root clean; sits with domain.md | Diverges from Matt skill default path; two places to teach |
| **C. Code enums as authority** | Generate glossary from `FailureKind`, `EvalRole`, protos | Always matches code | Misses architectural nouns (Task/Effect/Oracle); enums are not definitions |
| **D. Codegraph / search as authority** | Treat symbol graph or memory as ontology | Reuses tooling | Wrong layer; symbols ≠ domain language; pre-process path already removed |
| **E. External EvoOntology service** | New dependency / remote graph | Closest to named aspirational style | Heavy; flags/compliance; out of standing prefs |

**Recommended authority:** Option A — `CONTEXT.md` is the **only writer-of-record** for term definitions. MCP is a **read projection**. ADR files (`docs/adr/`) remain for surprising trade-offs, not for every term. Code enums may **cite** glossary terms but do not own prose.

### 2.4 MCP shape (MVP vs deferred)

**MVP (flag off by default):**

- Add read-only tools on the existing engine MCP (or a tiny sibling server registered the same way), e.g. `list_domain_terms`, `lookup_domain_term`.
- Behind something like `UC_DOMAIN_ONTOLOGY_MCP=1` (default unset/off) — same posture as Hy-Memory auto-context / NetworkMode enforce.
- Fail soft when flag off or file missing (tools hidden or empty), matching Hy-Memory MCP gating.
- No write tool; no auto-inject of glossary into every prompt (avoid surprise context bloat / drift).

**Deferred:**

- Write-back / evolve ontology from Agent runs (“Evo” half)
- Inference over relations, multi-context maps (`CONTEXT-MAP.md`)
- Replacing codegraph or Hy-Memory
- Auto-sync from code enums without human glossary edit
- Labeling UI / annotation ontology for D5 CV data

---

## 3. Fog item B — README / docs numeric recomputation CI (→ proposed D10)

### 3.1 Which README / docs numbers look like uplift or perf claims?

**Public uplift % in README:** **none today.** README talks about “measured improvements” qualitatively; D1/T2/`UC_REPORT_UPLIFT_ENFORCE` already forbid publishing select/search/approx as uplift. The fog item is partly **preventive** (keep README honest when figures appear) and partly **live numbers that already exist**.

**Concrete numeric claims that look like measured perf (high priority for a guard):**

| Claim | Where | Recomputable? |
|-------|--------|----------------|
| Steady max **6.394ms**, burst max **55.712ms**; 3 clients × 300 events; no loss/dup | `README.md` ~L169, `README.zh-CN.md` ~L156, detailed table in `docs/live-feedback-and-metainfer-verification.md` | Yes via `scripts/verify-sse-latency.py` + checked-in evidence under `.trellis/tasks/10-02-metainfer-architecture/live-verification.json` (doc points here) |
| Table p99 / max rows (2.260ms / 6.394ms; 55.270ms / 55.712ms) | `docs/live-feedback-and-metainfer-verification.md` | Same evidence / script |
| “Eleven independent workflows” / CI path table | README CI section | **Already guarded** by `scripts/check-readme-ci-table.py` (not uplift; structural) |

**Out of D10 MVP (defer):**

- GPU / MetaInfer `improvement_pct` production claims (map Out of scope: no production GPU perf promises)
- Re-running full `BenchmarkRunner` / Oracle in PR CI
- Pinning every env default port / version string in README (version pins are a different guard class)

Journal debt (T38 «账 5»): README `Checks` column prose vs job names was explicitly **not** guarded — leave that as optional follow-on, not D10 MVP.

### 3.2 Existing scripts that could recompute or pin

| Script / workflow | What it does | Fit for D10 |
|-------------------|--------------|-------------|
| `scripts/check-readme-ci-table.py` + `ci-readme-ci-table.yml` | Hard-fail reconcile README CI table ↔ workflow YAML (10 judgments; exit code IS verdict) | **Pattern template** for docs↔machine surface; does **not** cover latency/uplift figures |
| `scripts/check-spec-refs.py` (+ `ci-scripts.yml`) | Structural path:line audit under `.trellis/spec/` + `docs/`; hard fail on MISSING/PATH_FORM/OUT_OF_RANGE; advisory stale | Pattern for **docs drift** guards; advisory vs hard split |
| `scripts/check-workflow-inputs.py` | Workflow input coverage | Adjacent CI hygiene |
| `scripts/verify-sse-latency.py` | Live NATS→Dashboard→SSE measure; emits JSON with max/p99 | **Recompute engine** for the only README perf claims |
| `scripts/verify-metainfer-*.py` / `verify-local-deployment.py` | Containment / release / local deploy | Not uplift figure publishers |
| `published_uplift_verdict` / `refuse_approx_sourced_uplift` (Python inference) | Runtime publish boundary | Enforces **who may mint** uplift; does not sync README text |

### 3.3 CI failure strategy options

| Strategy | Meaning | When to use |
|----------|---------|-------------|
| **Hard fail on pin mismatch** | Checked-in README/doc numbers must equal golden evidence / manifest | Cheap, deterministic — **MVP** |
| **Advisory warn** | Comment / non-blocking check | Only for heuristics (like check-spec-refs advisory stale) |
| **Live remeasure in PR CI** | Run `verify-sse-latency` on every PR | Flaky/costly (needs NATS+dashboard); **defer** or manual workflow only |
| **Refuse undocumented figures** | Lint: any new `%` / `ms` uplift-like claim without a manifest entry fails | Good stretch goal after inventory exists |

Standing prefs already say new modules default **flags off**; for a **docs guard**, the established pattern is **always-on hard fail** once the closed input set is cheap (`check-readme-ci-table`, not a product feature flag). Prefer that for pin checks. Use a feature flag only if live remeasure is ever wired.

### 3.4 Recommended MVP shape

1. **Inventory manifest** (e.g. `docs/architecture/agent-trust-eval/numeric-claims.json` or `scripts/readme_numeric_claims.json`): list path + regex/span + expected value + `source_evidence` path + `role` (`perf_measurement` \| `structural` \| `forbidden_without_report`).
2. **Checker** `scripts/check-readme-numeric-claims.py`: read-only compare docs text ↔ manifest/evidence; non-vacuity + hard fail on drift (clone of readme-ci-table judgment style).
3. **CI**: dedicated path-filtered workflow (README + claim docs + script + evidence), same rationale as `ci-readme-ci-table.yml`.
4. **Seed claims:** the SSE 6.394 / 55.712 family only. Document that public uplift `%` must not appear unless minted under report-role helpers + listed in the manifest.
5. **Defer:** automatic rewrite of README from live runs; GPU uplift recomputation; turning live SSE into required PR CI.

---

## 4. Proposed resolutions (D7/D8 style)

### D9 — Project ontology via MCP

**Status:** proposed (not yet decided)

**Question:** 项目本体以什么为权威、经 MCP 暴露哪份最小词表？是否要做到 EvoOntology 式可演化图，还是先只读术语表？

**Context:**

- Map fog + assessment P2 fog row; intentionally out of `spec-p2.md`.
- No `CONTEXT.md` / `docs/adr/` yet; `docs/agents/domain.md` already names CONTEXT as canonical vocabulary.
- MCP stack exists (engine search/memory/Hy-Memory; codegraph symbol tools) but nothing serves domain terms.
- Codegraph ≠ ontology; Hy-Memory ≠ glossary.

**Proposed resolution:**

**MVP = read-only glossary MCP over `CONTEXT.md`; evolution deferred.**

1. Author a **minimal** `CONTEXT.md` vocabulary (runtime entities + trust/eval nouns above); humans own edits.
2. Expose **read-only** MCP lookup/list behind a **default-off** flag; fail soft when off/missing.
3. **Do not** treat codegraph, layered memory, or Hy-Memory as the ontology authority.
4. **Defer** write-back, auto-evolution, multi-context maps, and any Agent-authored term minting without human merge.

### D10 — README / docs numeric recomputation CI

**Status:** proposed (not yet decided)

**Question:** README/文档里的测量数字由哪条脚本复算？漂移是告警还是硬失败？哪些数字进入第一刀？

**Context:**

- No uplift `%` in README today; the live SSE localhost maxima (6.394ms / 55.712ms) are the main measured perf claims.
- `check-readme-ci-table.py` already hard-fails structural CI-table drift; `verify-sse-latency.py` can remeasure but needs a live stack.
- D1/T2 already gate *minting* uplift; docs text is still an ungarded publish surface for copied numbers.

**Proposed resolution:**

**MVP = pin known measured claims to evidence (hard fail); live remeasure deferred.**

1. First slice: **SSE latency figures** (and their doc table twins) via a manifest + checker modeled on `check-readme-ci-table` (closed input set, hard fail, non-vacuity).
2. Public **uplift `%`**: banned from README/docs unless report-role + manifest entry (align D1/T2); no GPU bench recompute in this decision.
3. **CI posture:** hard fail on pin drift for the closed set; **do not** require live `verify-sse-latency` on every PR (optional manual/nightly later).
4. **Defer:** auto-updating README from runs; production perf SLAs; full BenchmarkRunner recomputation CI.

---

## 5. Feasible approach summary (for PRD / grilling)

### D9 approaches

| | Approach | Notes |
|---|----------|-------|
| **A (Recommended)** | CONTEXT.md + read-only MCP, flag off | Matches domain.md; smallest ship |
| B | docs/agents/glossary.md + same MCP | Fine if root CONTEXT refused; teach domain.md |
| C | Enums-only generated glossary | Too narrow; reject as sole authority |

### D10 approaches

| | Approach | Notes |
|---|----------|-------|
| **A (Recommended)** | Manifest pin + hard-fail checker for SSE claims | Cheap; mirrors existing README CI guard |
| B | Advisory-only bot comments on drift | Weaker than repo norms for closed sets |
| C | Live SSE in PR CI | Flaky; defer |

---

## 6. Suggested decision-ticket skeletons (after adopt)

- `docs/architecture/agent-trust-eval/issues/D9-project-ontology-mcp.md`
- `docs/architecture/agent-trust-eval/issues/D10-readme-numeric-ci.md`
- Update map: move items from «Not yet specified» → «Decisions so far»; clear fog wording in assessment §五/§六
- Implementation backlog (post-decision, separate `$to-spec` / tickets): seed CONTEXT.md; MCP tools + flag; numeric manifest + checker + workflow

**Out of this grounding task:** implementing MCP/CI code; GitHub wayfinder publication; T18 true replan; D5 labeling pipeline fog.

---

## 7. Key file index

- Map / assessment: `docs/architecture/agent-trust-eval/map.md`, `docs/architecture/agent-trust-eval-assessment.md`
- Style refs: `docs/architecture/agent-trust-eval/issues/D7-stall-replan-ownership.md`, `D8-sandbox-isolation-target.md`
- Domain pointer: `docs/agents/domain.md`
- Entity prose: `docs/agent-os-architecture.md`
- MCP: `python/ultimate_coders/agent/engine_mcp.py`
- Codegraph spec: `.trellis/spec/backend/codegraph-integration.md`
- README claims: `README.md`, `README.zh-CN.md`, `docs/live-feedback-and-metainfer-verification.md`
- Guards / measure: `scripts/check-readme-ci-table.py`, `scripts/check-spec-refs.py`, `scripts/verify-sse-latency.py`
- CI: `.github/workflows/ci-readme-ci-table.yml`, `ci-scripts.yml`
