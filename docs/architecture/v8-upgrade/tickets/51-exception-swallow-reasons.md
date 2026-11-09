# 51 — Broad silent handlers carry a written reason (T51)

**What to build:** Audit `except Exception/BaseException/bare: pass` across
`python/ultimate_coders` — an AST pass found **11** — annotate each with a
`# best-effort: <reason>`, and turn the rule into a guard.

The rule is deliberately **not** "never swallow": most of those 11 are legitimate
best-effort paths (a closing client, a destructor during interpreter shutdown, a
diagnostics-only stderr drain, an advisory LSP `didClose`, receipt bookkeeping that is
re-derived from `remote_jobs`). The problem is that a reader and a reviewer cannot tell a
deliberate swallow from a bug, so the rule is "**swallow out loud**": a broad silent
handler carries a written reason; a **narrow** catch (`FileNotFoundError`, `TimeoutError`,
...) needs none, because the exception type is the explanation.

**Status:** implemented (`scripts/check-exception-swallow-reasons.py`,
`tests/python/test_check_exception_swallow_reasons.py`, 11 annotated handlers across
`agent/`, `dashboard/`, `engine.py`, `inference/adapter.py`,
`.github/workflows/ci-scripts.yml`, `README.md`)

- [x] 11 handlers annotated with a reason that names why swallowing is correct
- [x] Guard reports unexplained broad `pass` handlers (marker on the handler line, in its
      body, or up to four lines above the `try`)
- [x] Narrow catches and handlers that log/return are out of scope
- [x] Negative controls (unexplained, bare, narrow, logging) + real-repo clean + non-vacuity
- [x] Runs in Scripts CI (3.9 + 3.12) next to the other guards; README table updated

**Boundary:** this is a *visibility* rule, not a correctness proof — it forces the
judgement to be written down, and it does not attempt to decide whether a given swallow is
the right call.
