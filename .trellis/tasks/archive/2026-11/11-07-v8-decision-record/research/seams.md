# Seams for Decision Record

- Pattern donor: `python/ultimate_coders/context_outbox.py` + worker `_record_context_manifest`
- Dashboard donor: `app.py` `context_manifests_api` (~L373)
- Route payload: `RouteDecision.to_dict()` in `python/ultimate_coders/agent/routing.py`
- Runtime state: `python/ultimate_coders/runtime_state.py` (`mutate` / `query`)
- Digest: `hashlib.sha256(json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()).hexdigest()`
- Terminal hook points: search worker for `_publish_event` success/failure and `_save_checkpoint` callers; prefer one helper `_record_decision_terminal` near result finalization
