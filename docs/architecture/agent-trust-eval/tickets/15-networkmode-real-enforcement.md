# 15: NetworkMode real enforcement

**What to build:** Enforce `None` / `Restricted` / `Full` at **agent-subprocess** spawn when `UC_NETWORK_MODE_ENFORCE` is on. Kill fake safety: if enforce is on and the platform cannot apply the mode, refuse start. Do not cut NATS/Gateway on the worker process. Landlock FS is out of scope (ticket 17).

**Blocked by:** None.

**Status:** implemented (branch `cursor/t15-networkmode-real-enforcement-7c92`; planner in `python/ultimate_coders/agent/network_mode.py`; wire in `python/ultimate_coders/agent/sandbox.py` `_execute_subprocess`; Restricted fail-closed until T16 applicator; engine `execute_in_sandbox` bypass closed when enforce on; flag default off; tests in `tests/python/test_network_mode_enforce.py`)

- [x] Agent child respects None / Restricted / Full when enforce is on
- [x] Worker control-plane connectivity remains available
- [x] Unsupported platform + enforce on → fail closed (no silent Full)
- [x] Flag off leaves today’s spawn behaviour compatible
- [x] Tests cover at least None vs Full with a child egress probe (or documented CI skip matrix)
- [x] Feature flag defaults off (`UC_NETWORK_MODE_ENFORCE`)
