# Add types.py test coverage (Task/WorkflowStep serialization)

## Goal

`python/ultimate_coders/agent/types.py` has `Task.to_dict`/`from_dict` + `WorkflowStep.to_dict`/`from_dict` (serialization for checkpoint persistence) but NO test file. Add coverage, including the `verify_command` exclusion invariant (#561 — verify_command is runtime metadata, intentionally excluded from to_dict/from_dict).

## What I already know

* `Task.to_dict` (line 236) + `Task.from_dict` (line 290) — checkpoint serialization.
* `WorkflowStep.to_dict` (line 189) + `WorkflowStep.from_dict` (line 202).
* `Task.verify_command` (added #561) — intentionally NOT in to_dict/from_dict (runtime metadata, not persistent).
* `SubtaskResult.to_dict`/`from_dict` may exist too (check).
* `MemoryEntry.from_rust`/`from_dict` (memory.py) — separate module, out of scope.

## Requirements

* New `tests/python/test_types.py` covering: Task.to_dict (shape + fields), Task.from_dict (roundtrip), Task.to_dict excludes verify_command, WorkflowStep.to_dict/from_dict (roundtrip), edge cases (empty subtasks, None fields).
* pytest + ruff pass.

## Acceptance Criteria

* [ ] test_types.py created
* [ ] Task to_dict/from_dict roundtrip tested
* [ ] verify_command exclusion invariant tested
* [ ] WorkflowStep to_dict/from_dict tested
* [ ] pytest + ruff pass
* [ ] CI green

## Technical Notes

* `python/ultimate_coders/agent/types.py` — the module (Task, Subtask, SubtaskResult, WorkflowStep, etc.)
* #561 — verify_command field addition + exclusion from serialization
