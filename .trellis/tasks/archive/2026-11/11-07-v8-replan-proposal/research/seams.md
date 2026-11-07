# Seams

- Python kinds: `proactive/evaluator.py` ProposalKind + `_stall_candidates`
- Bridge: `agent/proactive_bridge.py` `propose_on_stall` / `evaluation_context`
- Orchestrator: `_propose_on_stall` always passes `action="stop"` today — fix to pass verdict
- Rust: `crates/uc-types/.../proactive.rs` ProposalKind + `uc-grpc/.../proactive_service.rs` `parse_proposal_kind`
- WatchdogAction fields: `preserve_effects`, `replans_remaining` (see watchdog_bridge / uc-types)
