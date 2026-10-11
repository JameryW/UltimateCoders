# Contained MetaInfer service extension

This separately deployed Python entrypoint adds the workspace and stop portions
of [the UC contract](metainfer-service-contract.md) to MetaInfer revision
b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890. UC does not vendor or fork upstream.
It reuses the pinned shell's discovery, create and control routes and runs each
orchestrator in a new immutable OCI container. The service is an execution API;
the UC Dashboard remains the product UI.

The current image contains upstream's Python dependencies. Supply a separately
built image and explicit coding-model environment for specialized GPU tasks.
The hardware endpoint currently returns 503: GPU/model identity and the full
optimization release gate remain unverified. Hardware attestation is not yet
implemented; adding a GPU image or --gpu does not enable it. Passing the CPU containment gate
does not establish Oracle acceptance, GPU performance or artifact delivery.

## Build and start

Use local Linux Podman or a rootful Docker daemon on a local Unix socket. Remote
daemons and Docker Desktop's separate VM cannot supply the required local kernel
evidence. Windows verification uses Podman inside WSL. Rootless Podman requires
the service and worktree owner to have the same UID/GID; rootless Docker is not
supported by this adapter.

From the UC repository, with its ordinary Python dependencies installed:

```bash
git clone --no-checkout https://github.com/HuangPuStar/MetaInfer.git /srv/metainfer/source
git -C /srv/metainfer/source checkout --detach b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890
python -m pip install -r /srv/metainfer/source/requirements.txt
podman build -f docker/Dockerfile.metainfer-job -t uc-metainfer-job:b3f6505 .

PYTHONPATH=python python -m ultimate_coders.metainfer_service \
  --source /srv/metainfer/source \
  --root /srv/metainfer/tasks \
  --control /srv/metainfer/control \
  --workspace-root /srv/uc-worktrees \
  --backend-id metainfer-local-a \
  --image uc-metainfer-job:b3f6505 --port 48766
```

The source checkout must be clean, including untracked files. The image must
declare the same upstream revision; its observed content ID, rather than its
tag, is used for every create. Source, task tree and control store must be
disjoint from all allowed workspace roots. The control store must also be
outside every container mount. Keep these paths and the backend ID stable on
restart. Each mutating repository must be an exclusive linked Git worktree.
Workspace roots are fixed to their canonical paths at startup; later symlink
changes cannot extend the authorized roots.
The control-store path is fixed the same way; changing its configured alias
cannot redirect private ownership records into a writable workspace.

The service binds to 127.0.0.1. Configure UC_METAINFER_URL on the Worker and
planner to that endpoint and UC_METAINFER_REVISION to the pinned revision.
Cross-host access can use an authenticated SSH tunnel; the worktree still needs
the same absolute path on both hosts. Pass a JSON object of string-valued job
environment variables with --job-env. Host credentials are not inherited.
The --gpu wsl option exposes /dev/dxg and read-only WSL libraries; --gpu nvidia
uses the local runtime's NVIDIA GPU support. These switches do not add CUDA,
Triton, a coding CLI or model configuration to the image.

## Writer ownership and recovery

The job image includes a small UC publisher alongside the unchanged upstream
source. Image label uc.metainfer.publisher=1 advertises kernel_publication.
For evolve-kernel, the contained wrapper copies the successful run's first
ranked, measured library entry into the assigned kernel_file_path and records
its SHA-256 in run.uc_candidate. The copy is atomic, rejects symlink/Git metadata
targets, and stays inside the existing OCI writer scope. It does not execute the
candidate, commit code or claim Oracle acceptance.

UC kernel workflows require this capability, wait for all-writer quiescence,
refetch the final outcome and verify the repository bytes against the receipt.
The protected UC benchmark and Oracle then decide acceptance or rollback.
Upgrade the job image and restart the service together; old images retain
workspace/lifecycle checks but cannot run these kernel workflows. Remote
worker_nodes delegation is refused because its writers would fall outside the
service's stop proof.

Containers run as the worktree's UID/GID with a read-only root filesystem,
private PID/cgroup namespaces, no capabilities, no new privileges and no
automatic restart. Only the worktree and the task's state/generated directories
are writable. Git common metadata, the per-worktree git directory and the .git
pointer are mounted read-only; UC owns final commits.

Launch intent is durable before create; immutable container identity is durable
before start; the host boot ID and owned kernel cgroup are durable before the
launch gate lets the orchestrator execute. Restart observes that same identity
without repeating create/start. Kill is only an acknowledgement. A stop receipt
requires identity and isolation checks, a stopped container, and no processes
in any descendant cgroup. Permission errors and unknown scopes remain unresolved.

Receipts are written durably before the transactional workspace claim is
released. A repeated receipt from an old job cannot release a subsequent job's
claim. Query GET /api/uc/jobs/TASK_ID/quiescence for the proof. Do not reuse task
IDs or delete ownership records to recover an unknown launch; retain the scope
for operator reconciliation. Completed containers and receipts are retained for
inspection; this version has no automatic retention cleanup.

The API keeps discovery/schema, create, kill, task listing and fixed-file task
state. Restart/reset/cluster mutations return 409. Optional plugin evidence and
agent-log routes return 404. Fixed metadata reads use no-follow directory/file
descriptors, accept bounded regular files, and return 503 for unsafe files.
Upstream app startup, native process reconciliation and plugin janitors are not
invoked, so they cannot create host writers outside the OCI scope.

## Reproduce containment checks

```bash
PYTHONPATH=python python scripts/verify-metainfer-containment.py \
  --root /srv/uc-verification/new-disposable-run \
  --image uc-metainfer-job:b3f6505

PYTHONPATH=python python scripts/verify-metainfer-release.py \
  --url http://127.0.0.1:48766 --workspace /srv/uc-worktrees/assigned-task \
  --revision b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890 \
  --output /srv/uc-verification/workspace.json
```

The first command requires a new disposable root. Two separate launcher
processes start and recover a detached writer, then prove cancellation stopped
it. The second command reads and writes a random nonce through an actual OCI
job, waits for its stop proof, and verifies the same bytes from UC. Its temporary
nonce directory is sticky and public to support different Worker/job UIDs;
probe bytes contain no secrets. A failed probe confirms stop before releasing
its claim, or retains the claim and reports its probe ID for reconciliation.
Add --require-gpu to the release command to enforce GPU identity; it currently
rejects this CPU extension with HTTP 503.

Measured deployment results are in
[the live report](live-feedback-and-metainfer-verification.md).
