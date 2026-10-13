"""Version-pinned Launcher adaptation, without modifying upstream task code."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .app import ServiceConfig, add_contract_routes, kernel_publication_enabled
from .jobs import ContainedJobs, checkpoint
from .observations import contain_observations, read_json
from .oci import LocalOCI

PINNED_REVISION = "b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890"
logger = logging.getLogger(__name__)


def restrict_mutations(app: FastAPI) -> None:
    @app.middleware("http")
    async def contained_mutations(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            path = request.url.path
            allowed = request.method == "POST" and path in (
                "/api/uc/workspaces/verify",
                "/api/sys-shell/tasks",
            )
            if (
                request.method == "POST"
                and path.startswith("/api/sys-shell/")
                and path.endswith("/control")
            ):
                try:
                    body = await request.json()
                    allowed = isinstance(body, dict) and body.get("action") == "kill"
                except ValueError:
                    return JSONResponse({"detail": "Invalid control request"}, status_code=400)
            if not allowed:
                return JSONResponse(
                    {"detail": "Only contained create and kill are enabled"}, status_code=409
                )
        return await call_next(request)


class UpstreamLauncher:
    def __init__(self, jobs: ContainedJobs, environment: dict[str, str]):
        self.jobs, self.environment = jobs, environment

    def start(
        self,
        task_id: str,
        requirements: dict,
        state_dir: Path,
        workspace_dir: Path,
        extra_args: list | None = None,
    ) -> int:
        from metainfer.orchestrator.registry import get_orchestrator
        from metainfer.server import paths

        if state_dir.resolve() != paths.task_dir(task_id).resolve() or (
            workspace_dir.resolve() != paths.workspace_dir(task_id).resolve()
        ):
            raise ValueError("Upstream task directories do not match the task identity")
        raw = json.loads(requirements["raw_request"])
        label = requirements.get("label", "")
        submission_identity = None
        if isinstance(label, str) and label.startswith("UC "):
            submission_identity = {
                "operation_id": label[3:],
                "request_sha256": hashlib.sha256(
                    json.dumps(raw, sort_keys=True).encode()
                ).hexdigest(),
            }
        if (
            getattr(self.jobs, "hardware_attestation", None) is not None
            and requirements["task_type"] != "evolve-kernel"
        ):
            raise ValueError("The attested native coding profile supports evolve-kernel only")
        # The pin also reads legacy form/answers. None may delegate writers
        # beyond the OCI scope proved by this service's cancellation contract.
        for fields in (requirements, requirements.get("form", {}), requirements.get("answers", {})):
            workers = fields.get("worker_nodes") if isinstance(fields, dict) else None
            if workers and (not isinstance(workers, str) or workers.strip()):
                raise ValueError("Remote workers are outside the owned OCI execution scope")
        repository = self.jobs.config.workspace(raw["repository"])
        if extra_args:
            raise ValueError("Arbitrary orchestrator arguments are disabled")

        # Git control metadata stays read-only; only UC owns host-side commits.
        def git_path(option: str) -> Path:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "safe.directory=" + str(repository),
                    "-C",
                    str(repository),
                    "rev-parse",
                    option,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return (repository / result.stdout.strip()).resolve(strict=True)

        git_dir, common = git_path("--git-dir"), git_path("--git-common-dir")
        if (
            not (repository / ".git").is_file()
            or git_dir.parent != common / "worktrees"
            or Path((git_dir / "gitdir").read_text().strip()).resolve() != repository / ".git"
        ):
            raise ValueError("Mutating jobs require an exclusive linked git worktree")
        entry = get_orchestrator(requirements["task_type"])
        state_dir.mkdir(parents=True, exist_ok=True)
        req_path = state_dir / "requirements.json"
        checkpoint(req_path, requirements)
        command = [
            "python",
            "-m",
            entry.cli_module,
            "run",
            str(req_path),
            "--state-dir",
            str(state_dir),
            "--workspace-dir",
            str(workspace_dir),
        ]
        if requirements["task_type"] == "evolve-kernel" and kernel_publication_enabled(self.jobs):
            if entry.cli_module != "metainfer.tasks.evolve_kernel.orchestrator.cli":
                raise ValueError("Unsupported kernel publisher module")
            command = [
                "python",
                "-m",
                "ultimate_coders.metainfer_service.execution",
                "--upstream-module",
                entry.cli_module,
                "--requirements",
                str(req_path),
                "--repository",
                str(repository),
                "--state-dir",
                str(state_dir),
                "--generated-dir",
                str(workspace_dir),
            ]
        return self.jobs.launch(
            task_id,
            repository,
            state_dir,
            workspace_dir,
            command,
            {**self.environment, "METAINFER_TASK_ID": task_id},
            extra_mounts=[(common, False), (git_dir, False), (repository / ".git", False)],
            **(
                {"submission_identity": submission_identity}
                if submission_identity is not None else {}
            ),
        )

    def status(self, task_id: str):
        from metainfer.server import paths
        from metainfer.server.launcher import ProcStatus

        try:
            status = self.jobs.status(task_id)
            if not status["running"]:
                if not self.jobs.quiescence(task_id)["writers_stopped"]:
                    raise RuntimeError("Descendant writers remain unresolved")
                run_path = paths.task_dir(task_id) / "run.json"
                run = read_json(run_path.parent, run_path.name, {})
                if not isinstance(run, dict):
                    raise ValueError("Invalid run metadata")
                if run.get("finished") is not True:
                    checkpoint(
                        run_path,
                        {
                            **run,
                            "finished": True,
                            "final_status": "failed",
                            "error": "Contained orchestrator exited without a final outcome",
                        },
                    )
        except (OSError, ValueError, RuntimeError):
            logger.warning("Contained execution state unavailable for %s", task_id, exc_info=True)
            # A rejected/uncertain submission remains visible; it must not break
            # the entire registry listing or acquire a false quiescence proof.
            status = {
                "running": False,
                "pid": None,
                "started_at": None,
                "finished_at": None,
                "exit_hint": "execution-unknown",
            }
        return ProcStatus(**status)

    def kill(self, task_id: str, force: bool = False) -> bool:
        return self.jobs.stop(task_id, force)


def create_service(
    source: Path,
    root: Path,
    config: ServiceConfig,
    runtime: LocalOCI,
    environment: dict[str, str],
    *,
    attest_ollama: bool = False,
    ollama_observe_url: str | None = None,
) -> FastAPI:
    source = source.resolve(strict=True)
    root, control = root.resolve(), config.control_dir.resolve()
    if root == control or root in control.parents or control in root.parents:
        raise ValueError("Control store and upstream task tree must not overlap")
    for allowed in config.workspace_roots:
        if any(
            allowed == protected or allowed in protected.parents or protected in allowed.parents
            for protected in (source, root, control)
        ):
            raise ValueError("Workspace roots must not overlap source, task tree or control store")
    revision = subprocess.run(
        ["git", "-c", "safe.directory=" + str(source), "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    dirty = subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=" + str(source),
            "-c",
            "core.autocrlf=true",
            "-C",
            str(source),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if revision != config.revision or revision != PINNED_REVISION or dirty:
        raise ValueError("MetaInfer source must be clean and match the supported revision")
    if runtime.image_labels.get("uc.metainfer.revision") != revision:
        raise ValueError("Job image does not declare the configured upstream revision")
    os.environ["METAINFER_ROOT"] = str(root.resolve())
    os.environ["METAINFER_NODE_ID"] = "uc-contained"
    os.environ["METAINFER_DISABLE_JANITOR"] = "1"
    sys.path.insert(0, str(source))
    from metainfer.server import launcher

    jobs = ContainedJobs(config, runtime)
    job_environment = {
        **environment,
        "METAINFER_ROOT": str(root.resolve()),
        "METAINFER_NODE_ID": "uc-contained",
        "HOME": "/tmp",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if attest_ollama:
        jobs.enable_hardware(job_environment, observe_url=ollama_observe_url)
    launcher._DEFAULT = UpstreamLauncher(jobs, job_environment)
    # Reuse only the shell's public route constructor. The stock app factory
    # starts native reapers and plugin janitors outside our writer scope.
    import metainfer.tasks  # noqa: F401 -- register pinned forms/orchestrators
    from metainfer.tasks.sys_shell.server.routes import build_router

    app = FastAPI(title="Contained MetaInfer", docs_url=None, redoc_url=None)
    contain_observations(app, launcher._DEFAULT, build_router(None))
    restrict_mutations(app)
    add_contract_routes(app, config, jobs)
    app.state.uc_jobs = jobs
    return app
