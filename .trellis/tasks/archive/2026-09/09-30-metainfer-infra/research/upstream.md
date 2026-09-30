# Verified upstream interface

Primary source: https://github.com/HuangPuStar/MetaInfer/tree/b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890

Endpoint contracts were inspected at the pinned commit; upstream source is not vendored.

- GET `/api/sys-shell/task-types/{type}/schema`: normalized `fields` with key/required/default.
- POST `/api/sys-shell/tasks`: `{type, label, raw_request, answers}` returns task_id, workspace_dir, state_dir, pid.
- GET `/api/sys-shell/{id}`: `run.finished`, `run.final_status`, process `status.running`, `status.finished_at` plus requirements.
- POST `/api/sys-shell/{id}/control`: `{action: kill, force: true}`. No submission idempotency key exists, so never retry a POST automatically.
- Task-specific state graphs/iterations are evidence, not the UC adaptation graph or normalized benchmark contract.
- Verified forms: port-model requires model_params_path/target_framework_dir; evolve-kernel requires kernel_file_path; sglang-trace-analyze requires model_path/version/batch_sizes/gpu_model plus numeric defaults; gen-infer-framework requires target_model/target_hardware and generates a new runtime.
- Shared filesystem/NFS remote GPU workers are supported via worker_nodes where the live form exposes it. MetaInfer is a trusted execution service; UC benchmarks and acceptance gates remain authoritative.

Windows process containment follows Microsoft's [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) and [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject) contracts. Nested jobs contain ordinary child processes, and KILL_ON_JOB_CLOSE terminates the contained tree. Both platforms gate actual command execution until containment/registration succeeds.
