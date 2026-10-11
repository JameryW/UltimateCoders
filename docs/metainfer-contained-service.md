# Contained MetaInfer service extension

This separately deployed Python entrypoint adds the workspace and stop portions
of [the UC contract](metainfer-service-contract.md) to MetaInfer revision
b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890. UC does not vendor or fork upstream.
It reuses the pinned shell's discovery, create and control routes and runs each
orchestrator in a new immutable OCI container. The service is an execution API;
the UC Dashboard remains the product UI.

The default image contains upstream's Python dependencies. Supply a CUDA/Triton
image and the explicit native Ollama profile below to enable contained hardware
attestation. The default CPU deployment returns HTTP 503 for hardware identity.
Real GPU/model preflight has passed; complete optimization, protected Oracle
acceptance and artifact delivery remain unverified under R8.

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

Workspace preflight advertises workspace_probe_identity. The caller selects a
probe-<32 lowercase hex> ID, and a successful contained challenge includes its
matching quiescence receipt. Exclusive durable claims reject duplicate IDs
before cleanup can touch another invocation. Querying an unknown probe's
quiescence fences future delayed submissions; an active pre-create claim remains
unresolved, while a failed launch without durable OCI intent can be fenced.

UC bounds complete workspace HTTP operations at 30s and hardware GET at 10s;
live GPU/model observations allow 5s/2s. On uncertain submission the client
retains nonces and a persistent cleanup_pending probe record, refuses replay,
and preserves the workspace. Confirm its saved probe ID stopped through the
versioned reconciliation CLI before recovering the workspace. Timeout alone
never permits rollback or release. confirm_stop leaves the retained nonce for
inspection: verify its saved directory is directly beneath the owned workspace,
remove it after matching stop proof, then recover the workspace lease and
explicitly retry. CPU/legacy successful probes remain compatible.

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
Add --require-gpu to enforce GPU identity. The default CPU deployment returns
HTTP 503; the explicit GPU profile below enables contained attestation.

## Explicit local Ollama GPU profile

Deploy matching service and job image code on a CUDA/Triton-capable base.
UC's optional native Ollama client uses the pinned upstream's Codex event
interface; it is not the OpenAI Codex CLI. Select it explicitly in --job-env:

```json
{
  "METAINFER_AGENT_BACKEND": "codex",
  "METAINFER_CODEX_BIN": "/opt/uc/ultimate_coders/metainfer_service/coding.py",
  "UC_OLLAMA_URL": "http://CONTAINER-REACHABLE-OLLAMA:11434",
  "UC_OLLAMA_MODEL": "YOUR-INSTALLED-MODEL:TAG",
  "UC_OLLAMA_DIGEST": "EXACT-64-CHARACTER-SHA256-FROM-OLLAMA"
}
```

Add --gpu wsl (or nvidia) and --attest-ollama to the service command. If the
same Ollama instance has a different host-side address, set
--ollama-observe-url http://HOST-REACHABLE-OLLAMA:11434. Both addresses must
observe the same model name and digest. This profile supports one physical and
visible GPU and evolve-kernel jobs. Unsupported environment overrides, changed
digests and incompatible images fail closed. URLs cannot contain credentials;
host proxy settings are not inherited. There is no automatic backend fallback.

Before serving, an owned image probe runs real Torch/Triton kernels and a model
nonce request. It executes no model-generated code. GPU UUID/driver, runtime,
model digest and image code are recorded after all probe writers stop. The native
client checks digest before and after requests, accepts actual tool_calls only,
bounds streams/sessions/commands and kills timed-out command groups. Coding
quality and optimization acceptance require separate verification.

Private cached evidence binds boot, image, profile, executable code and stopped
probe. Restart rechecks live identity without another GPU/model inference.
A changed profile cannot re-attest while any workspace remains owned, including
legacy ownership records. Start the service before GPU workers initially and
preserve its control store. Hardware GET and new launches refuse GPU/model drift.
Unavailable observations or uncertain probe writers cannot advertise hardware.

Measured deployment results are in
[the live report](live-feedback-and-metainfer-verification.md).
