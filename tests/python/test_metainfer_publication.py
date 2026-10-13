"""Public candidate publication boundary; generated code is never executed here."""

import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux OCI filesystem boundary")


@pytest.mark.parametrize("publisher", [False, True])
@pytest.mark.parametrize("label", ["UC experiment:1", "manual task"])
def test_launcher_negotiates_publication_inside_owned_job(tmp_path, monkeypatch, publisher, label):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ultimate_coders.metainfer_service.app import ServiceConfig, add_contract_routes
    from ultimate_coders.metainfer_service.upstream import UpstreamLauncher

    origin, workspace = tmp_path / "origin", tmp_path / "worktrees/job"

    def git(*args):
        subprocess.run(["git", *args], check=True, capture_output=True)

    git("init", str(origin))
    git(
        "-C",
        str(origin),
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=f@localhost",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    git("-C", str(origin), "worktree", "add", "--detach", str(workspace))
    state, generated = tmp_path / "state", tmp_path / "generated"
    monkeypatch.setitem(
        sys.modules,
        "metainfer.server",
        SimpleNamespace(
            paths=SimpleNamespace(task_dir=lambda _: state, workspace_dir=lambda _: generated)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "metainfer.orchestrator.registry",
        SimpleNamespace(
            get_orchestrator=lambda _: SimpleNamespace(
                cli_module="metainfer.tasks.evolve_kernel.orchestrator.cli"
            )
        ),
    )
    labels = {"uc.metainfer.publisher": "1"} if publisher else {}
    calls = []
    jobs = SimpleNamespace(
        config=ServiceConfig(tmp_path / "control", (workspace.parent,), "backend", "pin"),
        runtime=SimpleNamespace(image_labels=labels),
        launch=lambda *args, **kwargs: calls.append((args, kwargs)) or 42,
    )
    requirement = {
        "task_type": "evolve-kernel",
        "kernel_file_path": str(workspace / "kernel.py"),
        "raw_request": json.dumps({"repository": str(workspace)}),
        "label": label,
    }
    assert UpstreamLauncher(jobs, {}).start("job", requirement, state, generated) == 42
    command = calls[0][0][4]
    if label.startswith("UC "):
        assert calls[0][1]["submission_identity"] == {
            "operation_id": "experiment:1",
            "request_sha256": hashlib.sha256(
                json.dumps({"repository": str(workspace)}, sort_keys=True).encode()
            ).hexdigest(),
        }
    else:
        assert "submission_identity" not in calls[0][1]
    if publisher:
        assert command[2] == "ultimate_coders.metainfer_service.execution"
        assert command[command.index("--repository") + 1] == str(workspace)
    else:
        assert command[2] == "metainfer.tasks.evolve_kernel.orchestrator.cli"
    app = FastAPI()
    add_contract_routes(app, jobs.config, jobs)
    with TestClient(app) as client:
        capabilities = client.get("/api/uc/contract").json()["capabilities"]
    assert ("kernel_publication" in capabilities) is publisher


@pytest.mark.parametrize("workers", ["gpu-remote", ["gpu-remote"]])
@pytest.mark.parametrize("layout", ["flat", "form", "answers"])
def test_remote_workers_are_refused_before_workspace_claim(tmp_path, monkeypatch, workers, layout):
    from ultimate_coders.metainfer_service.upstream import UpstreamLauncher

    state, generated = tmp_path / "state", tmp_path / "generated"
    monkeypatch.setitem(
        sys.modules,
        "metainfer.server",
        SimpleNamespace(
            paths=SimpleNamespace(task_dir=lambda _: state, workspace_dir=lambda _: generated)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "metainfer.orchestrator.registry",
        SimpleNamespace(get_orchestrator=lambda _: None),
    )
    jobs = SimpleNamespace()
    requirements = {"raw_request": "{}"}
    requirements.update(
        {"worker_nodes": workers} if layout == "flat" else {layout: {"worker_nodes": workers}}
    )
    with pytest.raises(ValueError, match="Remote workers"):
        UpstreamLauncher(jobs, {}).start("job", requirements, state, generated)
    assert not state.exists()


def publication_fixture(tmp_path):
    repository, state, generated = (
        tmp_path / name for name in ("repository", "state", "generated")
    )
    for root in (repository, state, generated):
        root.mkdir()
    kernel = repository / "kernel.py"
    kernel.write_text("original kernel\n")
    (state / "run.json").write_text(json.dumps({"finished": True, "final_status": "success"}))
    (generated / "kernel_library.json").write_text(
        json.dumps([{"id": "candidate-a", "code": "candidate kernel\n", "exec_time_ms": 0.01}])
    )
    return repository, state, generated, kernel


def test_ranked_generated_kernel_reaches_repository_as_an_unaccepted_candidate(tmp_path):
    from ultimate_coders.metainfer_service.execution import publish_kernel

    repository, state, generated, kernel = publication_fixture(tmp_path)
    receipt = publish_kernel(repository, state, generated, kernel)
    assert kernel.read_text() == "candidate kernel\n"
    assert receipt["code_sha256"] == hashlib.sha256(kernel.read_bytes()).hexdigest()
    assert receipt["kernel_path"] == "kernel.py"
    assert receipt["oracle_acceptance"] == "pending"
    assert json.loads((state / "run.json").read_text())["uc_candidate"] == receipt


@pytest.mark.parametrize("failure", ["none", "upstream", "publication"])
def test_execution_cli_propagates_failed_publication_without_false_success(
    tmp_path, monkeypatch, caplog, failure
):
    from ultimate_coders.metainfer_service import execution

    repository, state, generated, kernel = publication_fixture(tmp_path)
    requirements = state / "requirements.json"
    requirements.write_text(
        json.dumps({"task_type": "evolve-kernel", "kernel_file_path": str(kernel)})
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "execution",
            "--upstream-module",
            "metainfer.tasks.evolve_kernel.orchestrator.cli",
            "--requirements",
            str(requirements),
            "--repository",
            str(repository),
            "--state-dir",
            str(state),
            "--generated-dir",
            str(generated),
        ],
    )
    monkeypatch.setattr(execution.subprocess, "call", lambda _: 1 if failure == "upstream" else 0)
    if failure == "publication":
        (generated / "kernel_library.json").write_text("[]")
    assert execution.main() == (0 if failure == "none" else 1)
    run = json.loads((state / "run.json").read_text())
    if failure == "none":
        assert run["uc_candidate"]["oracle_acceptance"] == "pending"
    else:
        assert run["final_status"] == "failed"
        assert "uc_candidate" not in run
        assert "Kernel candidate publication failed" in caplog.text


@pytest.mark.parametrize("status", ["running", "failed"])
def test_incomplete_or_failed_upstream_run_cannot_publish_code(tmp_path, status):
    from ultimate_coders.metainfer_service.execution import publish_kernel

    repository, state, generated, kernel = publication_fixture(tmp_path)
    (state / "run.json").write_text(
        json.dumps({"finished": status != "running", "final_status": status})
    )
    with pytest.raises(ValueError, match="successful terminal"):
        publish_kernel(repository, state, generated, kernel)
    assert kernel.read_text() == "original kernel\n"


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_unmeasured_or_invalid_library_entry_cannot_publish(tmp_path, value):
    from ultimate_coders.metainfer_service.execution import publish_kernel

    repository, state, generated, kernel = publication_fixture(tmp_path)
    (generated / "kernel_library.json").write_text(
        json.dumps([{"id": "candidate-a", "code": "candidate kernel\n", "exec_time_ms": value}])
    )
    with pytest.raises(ValueError, match="measured kernel"):
        publish_kernel(repository, state, generated, kernel)
    assert kernel.read_text() == "original kernel\n"


@pytest.mark.parametrize("kind", ["outside", "git", "leaf_symlink", "parent_symlink"])
def test_publication_cannot_overwrite_protected_or_redirected_paths(tmp_path, kind):
    from ultimate_coders.metainfer_service.execution import publish_kernel

    repository, state, generated, _ = publication_fixture(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    target = private / "kernel.py"
    target.write_text("private sentinel\n")
    if kind == "outside":
        kernel = target
    elif kind == "git":
        metadata = repository / ".git"
        metadata.mkdir()
        kernel = metadata / "config"
        kernel.write_text("private sentinel\n")
    elif kind == "leaf_symlink":
        kernel = repository / "alias.py"
        kernel.symlink_to(target)
    else:
        (repository / "alias").symlink_to(private, target_is_directory=True)
        kernel = repository / "alias/kernel.py"
    with pytest.raises(ValueError):
        publish_kernel(repository, state, generated, kernel)
    assert target.read_text() == "private sentinel\n"
    if kind == "git":
        assert kernel.read_text() == "private sentinel\n"
