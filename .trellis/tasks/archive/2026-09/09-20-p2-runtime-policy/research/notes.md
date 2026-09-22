# Evidence

- Baseline: 9be830d. #656 was the only open GitHub issue at discovery.
- Codex dispatch mode is inline. Implementation stays in this task.
- GraphStore::connect runs migrations; the report uses a plain PgPool instead.
- Attempt failures/timeouts persist FAILED; imports can preserve CANCELLED and
  SKIPPED. Report timing includes all four terminal status tokens.
- A failing execute_subtask test proved rejected JSON was previously success.
- Another failing test proved checkpoint replay lost the approved verdict.
- Capacity scoring keeps the existing roster gates, compares exact fractions,
  and is opt-in; default affinity keeps its previous precedence.
- Test seams: report/worker/registry. The sandbox mock is an external process
  substitute. No live model calls or external messages are needed.
- Docker is stopped and 127.0.0.1:5432 does not accept a connection.
  `postgres_runtime_report_uses_one_graph_and_never_counts_step_usage_twice`
  compiles (`cargo test -p uc-engine --test runtime_report_integration --no-run`)
  but has not been executed. Do not claim a live PostgreSQL run.
- 2026-09-22 verification: review/workflow pytest 77 passed; `runtime_metrics`
  3 passed; `uc-grpc` placement filter 30 passed; `runtime_report` example
  built with `--no-default-features --features storage`; `uc-grpc-server`
  `cargo check` passed. Ordinary non-aborting workflow success is preserved;
  review step prompts receive `REVIEW_INSTRUCTIONS` even without `{{context}}`.
- Full Python collection initially hit sandbox access denial on pydantic;
  the same test command was rerun outside sandbox without dependency changes.
- 2026-09-22 continuation: review/workflow pytest 72 passed; `runtime_metrics`
  3 passed; `uc-grpc` full lib 226 passed (placement filter 30); `cargo fmt
  --check` clean; `ruff check` clean; `cargo check -p uc-grpc-server` and
  storage example check passed; `runtime_report_integration --no-run`
  compiles (live PG test stays `#[ignore]`). `cargo clippy -D warnings`
  still fails on pre-existing `crates/uc-grpc/src/server.rs:3717`
  `let_unit_value` (file untouched by this task). Independent spec review:
  all three P2 scenarios PASS, no wrong-vs-correct violations, no over-claims.
