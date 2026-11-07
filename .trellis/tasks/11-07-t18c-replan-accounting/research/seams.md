# Seams

- Types: `crates/uc-types/src/governance/workcase.rs`; `WatchdogSignal` in proactive.rs
- Watchdog: `crates/uc-engine/src/governance/watchdog.rs`
- Stall path: `crates/uc-grpc/src/governance/proactive_service.rs` (today copies `request.replans_used`)
- Proto: `crates/uc-grpc/proto/engine.proto` WorkCase / stall messages
- Case store: governance case service load/update CAS
- Python compat: `watchdog_input.py`, `watchdog_bridge.py` — prefer gateway counters when flag on
- Tests: proactive_service watchdog tests; `test_proactive_watchdog.py`
