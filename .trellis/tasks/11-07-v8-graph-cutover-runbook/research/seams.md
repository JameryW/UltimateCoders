# Seams

- Gate donor: `crates/uc-grpc-server/src/main.rs` `resolve_graph_shadow_choice` (exact `"on"`)
- Shadow wiring: same file ~894–936 (`setup_graph_store` + `set_graph_shadow`)
- Ops context: `docs/architecture/durable-runtime-upgrade-notes.md`, `docs/architecture.md` § Durable Runtime
- Script donor pattern: `scripts/check-line-endings.py` / other `scripts/check-*.py` CLI + pytest
- V8 docs pattern: `docs/architecture/v8-upgrade/spec-p3.md` + tickets 10–12
