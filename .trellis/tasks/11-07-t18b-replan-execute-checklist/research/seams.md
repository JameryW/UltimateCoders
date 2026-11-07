# Seams

- Decide hook: `crates/uc-grpc/src/governance/proactive_service.rs` `decide_proposal`
- Attention donor: `escalate_to_attention` / `attention_service::raise_attention`
- Gate: `crates/uc-engine/src/governance/proactive.rs` `ProactiveGate::decide`
- Types: `ProposalKind::Replan` already exists
- Flag pattern: other `UC_*_ENFORCE` env parsers in gateway boot / Python
- Tests: uc-grpc governance proactive tests; `tests/python/test_dashboard_proactive_api.py` / `test_replan_proposal.py` donors
